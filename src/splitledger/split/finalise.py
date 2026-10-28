import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from splitledger.contracts.money import Cents
from splitledger.db import models
from splitledger.db.models import Allocation, Bill, BillItem, BillStatus, ItemClaim, Participant
from splitledger.split import engine

# what to do with items nobody claimed
LEFTOVERS = ("ask", "even", "mine")


# raised when items are unclaimed and the payer has not said what to do with them
class Leftovers(Exception):
    # keep the item names to show the payer
    def __init__(self, names: list[str]) -> None:
        super().__init__(f"unclaimed: {', '.join(names)}")
        self.names = names


# one person's final share of a closed bill
@dataclass(frozen=True)
class Share:
    person: Participant
    cents: int


# a closed bill with everyone's share
@dataclass(frozen=True)
class Closing:
    bill: Bill
    shares: list[Share]
    payer: Participant


# the payer's own share and what each friend owes, the payer covers people who never joined
def even_split(bill: Bill, people: list[Participant], payer: Participant) -> dict[uuid.UUID, int]:
    each = bill.total // (bill.even_ways or len(people))
    shares = {person.id: each for person in people if person.id != payer.id}
    shares[payer.id] = bill.total - sum(shares.values())
    return shares


# shares from claims through the split engine, leftovers shared by everyone or kept by the payer
def item_split(
    bill: Bill, items: list[BillItem], claims: list[ItemClaim], people: list[Participant],
    payer: Participant, leftover: str
) -> dict[uuid.UUID, int]:
    rows = [engine.Claim(str(c.bill_item_id), str(c.participant_id), c.weight) for c in claims]
    claimed = {c.bill_item_id for c in claims}
    loose = [item for item in items if item.id not in claimed]
    if loose and leftover == "ask":
        raise Leftovers([item.name for item in loose])
    takers = people if leftover == "even" else [payer]
    rows += [engine.Claim(str(item.id), str(person.id)) for item in loose for person in takers]
    split = engine.Bill(
        tuple(engine.Item(str(item.id), Cents(item.price_cents)) for item in items),
        Cents(bill.service), Cents(bill.tax), Cents(bill.discount), Cents(bill.total)
    )
    shares = engine.BillSplitter().allocate(split, rows)
    return {uuid.UUID(person): int(cents) for person, cents in shares.items()}


# a ledger row for a closed bill
def entry(
    bill: Bill, user: uuid.UUID, kind: models.LedgerKind, cents: int, **extra: object
) -> models.LedgerEntry:
    return models.LedgerEntry(
        user_id=user, kind=kind, amount_cents=cents, currency=bill.currency,
        category=bill.category or "Other", vendor=bill.vendor, source=models.LedgerSource.BILL,
        bill_id=bill.id, status=models.LedgerStatus.POSTED, **extra
    )


# a friend's expense and what they owe the payer, posted once they have an account
def friend_rows(
    bill: Bill, person: Participant, cents: int, payer_name: str
) -> list[models.LedgerEntry]:
    if person.user_id is None or not cents:
        return []
    return [
        entry(bill, person.user_id, models.LedgerKind.EXPENSE, cents),
        entry(
            bill, person.user_id, models.LedgerKind.PAYABLE, cents,
            counterparty_id=bill.payer_id, note=payer_name
        )
    ]


# post a friend's rows when they link after the bill closed, once
async def post_late(session: AsyncSession, bill: Bill, person: Participant) -> None:
    allocation = (await session.execute(
        select(Allocation).where(
            Allocation.bill_id == bill.id, Allocation.participant_id == person.id,
            Allocation.posted_at.is_(None)
        ).with_for_update()
    )).scalar_one_or_none()
    if allocation is None:
        return
    payer = (await session.execute(
        select(Participant.guest_name).where(
            Participant.bill_id == bill.id, Participant.user_id == bill.payer_id
        )
    )).scalar_one_or_none()
    session.add_all(friend_rows(bill, person, allocation.amount_cents, payer or "the payer"))
    allocation.posted_at = datetime.now(UTC)
    # the payer's row now knows who this is, so paying it back clears it
    await session.execute(
        update(models.LedgerEntry).where(
            models.LedgerEntry.bill_id == bill.id, models.LedgerEntry.user_id == bill.payer_id,
            models.LedgerEntry.kind == models.LedgerKind.RECEIVABLE,
            models.LedgerEntry.counterparty_id.is_(None),
            models.LedgerEntry.note == person.guest_name
        ).values(counterparty_id=person.user_id)
    )


# closes a bill once: shares, allocations and every ledger row in one transaction
class BillCloser:
    # sessions for bills and ledgers
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    # close an open bill, none when it was closed already so a retry posts nothing
    async def close(self, bill_id: uuid.UUID, leftover: str = "ask") -> Closing | None:
        if leftover not in LEFTOVERS:
            raise ValueError(f"leftover must be one of {', '.join(LEFTOVERS)}")
        async with self._sessions() as session:
            bill = (await session.execute(
                select(Bill).where(Bill.id == bill_id).with_for_update()
            )).scalar_one()
            if bill.status != BillStatus.OPEN:
                return None
            items = list((await session.execute(
                select(BillItem).where(BillItem.bill_id == bill.id).order_by(BillItem.position)
            )).scalars())
            people = list((await session.execute(
                select(Participant).where(Participant.bill_id == bill.id)
            )).scalars())
            claims = list((await session.execute(
                select(ItemClaim).where(ItemClaim.bill_item_id.in_([item.id for item in items]))
            )).scalars())
            payer = next(person for person in people if person.user_id == bill.payer_id)
            shares = (
                even_split(bill, people, payer) if bill.even_ways
                else item_split(bill, items, claims, people, payer, leftover)
            )
            now = datetime.now(UTC)
            rows = [entry(bill, bill.payer_id, models.LedgerKind.EXPENSE, shares.get(payer.id, 0))]
            for person in people:
                cents = shares.get(person.id, 0)
                if person.id == payer.id or not cents:
                    continue
                rows.append(entry(
                    bill, bill.payer_id, models.LedgerKind.RECEIVABLE, cents,
                    counterparty_id=person.user_id, note=person.guest_name
                ))
                posted = friend_rows(bill, person, cents, payer.guest_name)
                rows += posted
                session.add(Allocation(
                    bill_id=bill.id, participant_id=person.id, amount_cents=cents,
                    posted_at=now if posted else None
                ))
            session.add(Allocation(
                bill_id=bill.id, participant_id=payer.id, amount_cents=shares.get(payer.id, 0),
                posted_at=now
            ))
            session.add_all(rows)
            bill.status = BillStatus.FINALISED
            bill.finalised_at = now
            await session.commit()
        ordered = [Share(person, shares.get(person.id, 0)) for person in people]
        return Closing(bill, ordered, payer)
