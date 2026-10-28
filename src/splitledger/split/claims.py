import uuid
from dataclasses import dataclass
from fractions import Fraction

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from splitledger.db.models import Bill, BillItem, BillStatus, ItemClaim, Participant
from splitledger.handlers.bill import even_share

ACTIONS = ("mine", "share", "drop")
MAX_NAME = 40
TAKEN = "{} already has this. Tap Share it if you shared it."
CLOSED = "This bill is closed, so claims can't change."


# one line on the bill and who has claimed it
@dataclass(frozen=True)
class Line:
    id: uuid.UUID
    name: str
    qty: int
    price: int
    holders: list[str]
    mine: bool


# everything the claim page shows for one viewer
@dataclass(frozen=True)
class BillView:
    bill: Bill
    lines: list[Line]
    people: list[Participant]
    me: Participant | None
    share: int
    unclaimed: int


# what one person owes so far, service tax and discount spread by their share of the items
def preview_share(bill: Bill, items: list[BillItem], claims: list[ItemClaim], me: uuid.UUID) -> int:
    if not bill.subtotal:
        return 0
    weights: dict[uuid.UUID, Fraction] = {}
    for claim in claims:
        before = weights.get(claim.bill_item_id, Fraction(0))
        weights[claim.bill_item_id] = before + Fraction(str(claim.weight))
    mine = Fraction(0)
    for claim in claims:
        if claim.participant_id == me:
            price = next(item.price_cents for item in items if item.id == claim.bill_item_id)
            mine += Fraction(price) * Fraction(str(claim.weight)) / weights[claim.bill_item_id]
    exact = mine * bill.total / bill.subtotal
    return int(exact + Fraction(1, 2))


# reads and changes claims on a bill, one bill row lock per change so taps can't race
class ClaimBook:
    # sessions for bills, items, people and claims
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    # the page for one viewer, none when the code is unknown
    async def view(self, code: str, me_id: uuid.UUID | None) -> BillView | None:
        async with self._sessions() as session:
            query = select(Bill).where(Bill.code == code)
            bill = (await session.execute(query)).scalar_one_or_none()
            if bill is None:
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
        names = {person.id: person.guest_name for person in people}
        me = next((person for person in people if person.id == me_id), None)
        lines = [
            Line(
                item.id, item.name, item.qty, item.price_cents,
                [names[c.participant_id] for c in claims if c.bill_item_id == item.id],
                any(c.bill_item_id == item.id and c.participant_id == me_id for c in claims)
            )
            for item in items
        ]
        share = 0
        if me is not None and bill.even_ways:
            each, payer = even_share(bill.total, bill.even_ways)
            share = payer if me.user_id == bill.payer_id else each
        elif me is not None:
            share = preview_share(bill, items, claims, me.id)
        unclaimed = sum(1 for line in lines if not line.holders)
        return BillView(bill, lines, people, me, share, unclaimed)

    # the person with that name on the bill, added when new
    async def join(self, bill_id: uuid.UUID, name: str) -> uuid.UUID:
        name = " ".join(name.split())[:MAX_NAME]
        if not name:
            raise ValueError("a name is needed")
        async with self._sessions() as session:
            found = (await session.execute(
                select(Participant.id).where(
                    Participant.bill_id == bill_id,
                    func.lower(Participant.guest_name) == name.lower()
                )
            )).scalar_one_or_none()
            if found is not None:
                return found
            person = Participant(id=uuid.uuid4(), bill_id=bill_id, guest_name=name)
            session.add(person)
            await session.commit()
            return person.id

    # mine claims alone, share joins whoever has it, drop lets go, a message when refused
    async def claim(
        self, bill_id: uuid.UUID, me: uuid.UUID, item_id: uuid.UUID, action: str
    ) -> str | None:
        if action not in ACTIONS:
            raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
        async with self._sessions() as session:
            # every change to one bill waits its turn here
            bill = (await session.execute(
                select(Bill).where(Bill.id == bill_id).with_for_update()
            )).scalar_one()
            if bill.status != BillStatus.OPEN:
                return CLOSED
            item = await session.get(BillItem, item_id)
            person = await session.get(Participant, me)
            on_bill = item is not None and item.bill_id == bill_id
            if not on_bill or person is None or person.bill_id != bill_id:
                raise ValueError("item or person is not on this bill")
            if action == "drop":
                await session.execute(delete(ItemClaim).where(
                    ItemClaim.bill_item_id == item_id, ItemClaim.participant_id == me
                ))
                await session.commit()
                return None
            holders = list((await session.execute(
                select(Participant.guest_name, ItemClaim.participant_id)
                .join(Participant, ItemClaim.participant_id == Participant.id)
                .where(ItemClaim.bill_item_id == item_id)
            )).all())
            if any(holder == me for _, holder in holders):
                return None
            others = [name for name, holder in holders if holder != me]
            if action == "mine" and others:
                return TAKEN.format(others[0])
            session.add(ItemClaim(bill_item_id=item_id, participant_id=me))
            await session.commit()
        return None
