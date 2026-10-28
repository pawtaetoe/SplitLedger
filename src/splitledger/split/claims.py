import uuid
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from splitledger.db.models import Bill, BillItem, BillStatus, ItemClaim, Participant
from splitledger.handlers.bill import MAX_UNITS, even_share, unit_prices
from splitledger.llm.schemas import unescape
from splitledger.split.finalise import post_late

ACTIONS = ("mine", "share", "drop")
MAX_NAME = 40
TAKEN = "{} already has this. Tap Share it if you shared it."
CLOSED = "This bill is closed, so claims can't change."
ONLY_PAYER = "Only the person who paid can change how this bill is split."


# one line on the bill and who has claimed it
@dataclass(frozen=True)
class Line:
    id: uuid.UUID
    name: str
    qty: int
    price: int
    holders: list[str]
    mine: bool
    # the viewer's part of the line price, before service and tax
    part: int


# everything the claim page shows for one viewer
@dataclass(frozen=True)
class BillView:
    bill: Bill
    lines: list[Line]
    people: list[Participant]
    me: Participant | None
    share: int
    unclaimed: int
    # who paid and everyone's share so far, so the page shows the whole bill
    payer: Participant | None
    shares: dict[uuid.UUID, int]

    # lines numbered from 1 with neighbours of the same dish kept together
    def groups(self) -> list[list[tuple[int, Line]]]:
        grouped: list[list[tuple[int, Line]]] = []
        for number, line in enumerate(self.lines, 1):
            if grouped and grouped[-1][0][1].name == line.name:
                grouped[-1].append((number, line))
            else:
                grouped.append([(number, line)])
        return grouped


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


# one person's part of one line, by weight among everyone who ticked it
def part_of(item: BillItem, claims: list[ItemClaim], me: uuid.UUID | None) -> int:
    on_item = [c for c in claims if c.bill_item_id == item.id]
    mine = sum(c.weight for c in on_item if c.participant_id == me)
    total = sum(c.weight for c in on_item)
    return round(item.price_cents * mine / total) if mine else 0


# true when a bill made before units still has an unclaimed line of several or garbled names
def needs_tidy(items: list[BillItem], claims: list[ItemClaim]) -> bool:
    claimed = {claim.bill_item_id for claim in claims}
    return any(
        (1 < item.qty <= MAX_UNITS and item.id not in claimed) or unescape(item.name) != item.name
        for item in items
    )


# older bills get one row per unit and real letters, claimed lines are left alone
async def tidy(
    session: AsyncSession, bill: Bill, items: list[BillItem], claims: list[ItemClaim]
) -> list[BillItem]:
    await session.execute(select(Bill.id).where(Bill.id == bill.id).with_for_update())
    claimed = {claim.bill_item_id for claim in claims}
    tidied: list[BillItem] = []
    for item in items:
        item.name = unescape(item.name) or item.name
        if item.id in claimed or item.qty <= 1:
            tidied.append(item)
            continue
        rows = unit_prices(item.name, item.qty, item.price_cents)
        if len(rows) == 1:
            tidied.append(item)
            continue
        item.qty, item.price_cents = 1, rows[0][1]
        tidied.append(item)
        for name, cents in rows[1:]:
            extra = BillItem(id=uuid.uuid4(), bill_id=bill.id, name=name, qty=1, price_cents=cents)
            session.add(extra)
            tidied.append(extra)
    for position, item in enumerate(tidied):
        item.position = position
    await session.commit()
    return tidied


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
            claims = list((await session.execute(
                select(ItemClaim).where(ItemClaim.bill_item_id.in_([item.id for item in items]))
            )).scalars())
            if bill.status == BillStatus.OPEN and needs_tidy(items, claims):
                items = await tidy(session, bill, items, claims)
            people = list((await session.execute(
                select(Participant).where(Participant.bill_id == bill.id)
            )).scalars())
        names = {person.id: person.guest_name for person in people}
        me = next((person for person in people if person.id == me_id), None)
        lines = [
            Line(
                item.id, item.name, item.qty, item.price_cents,
                [names[c.participant_id] for c in claims if c.bill_item_id == item.id],
                any(c.bill_item_id == item.id and c.participant_id == me_id for c in claims),
                part_of(item, claims, me_id)
            )
            for item in items
        ]
        payer = next((person for person in people if person.user_id == bill.payer_id), None)
        if bill.even_ways:
            each, covers = even_share(bill.total, bill.even_ways)
            shares = {p.id: covers if p is payer else each for p in people}
        else:
            shares = {p.id: preview_share(bill, items, claims, p.id) for p in people}
        share = shares.get(me.id, 0) if me is not None else 0
        unclaimed = sum(1 for line in lines if not line.holders)
        return BillView(bill, lines, people, me, share, unclaimed, payer, shares)

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
        self, bill_id: uuid.UUID, me: uuid.UUID, item_id: uuid.UUID, action: str,
        weight: float = 1.0
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
            session.add(ItemClaim(bill_item_id=item_id, participant_id=me, weight=weight))
            await session.commit()
        return None

    # tick boxes saved at once: exactly these items are mine, shared with whoever else ticked
    async def set_mine(
        self, bill_id: uuid.UUID, me: uuid.UUID, item_ids: set[uuid.UUID]
    ) -> str | None:
        async with self._sessions() as session:
            bill = (await session.execute(
                select(Bill).where(Bill.id == bill_id).with_for_update()
            )).scalar_one()
            if bill.status != BillStatus.OPEN:
                return CLOSED
            on_bill = set((await session.execute(
                select(BillItem.id).where(BillItem.bill_id == bill_id)
            )).scalars())
            if not item_ids <= on_bill:
                raise ValueError("item is not on this bill")
            held = set((await session.execute(
                select(ItemClaim.bill_item_id).where(ItemClaim.participant_id == me)
            )).scalars())
            if held - item_ids:
                await session.execute(delete(ItemClaim).where(
                    ItemClaim.participant_id == me, ItemClaim.bill_item_id.in_(held - item_ids)
                ))
            session.add_all(
                ItemClaim(bill_item_id=item, participant_id=me) for item in item_ids - held
            )
            await session.commit()
        return None

    # the payer turns an even split into one where friends tick their own items
    async def to_items(self, bill_id: uuid.UUID, me: uuid.UUID) -> str | None:
        async with self._sessions() as session:
            bill = (await session.execute(
                select(Bill).where(Bill.id == bill_id).with_for_update()
            )).scalar_one()
            person = await session.get(Participant, me)
            if person is None or person.user_id != bill.payer_id:
                return ONLY_PAYER
            if bill.status != BillStatus.OPEN:
                return CLOSED
            bill.even_ways = None
            await session.commit()
        return None

    # the open bill with this code, none when unknown or closed
    async def open_bill(self, code: str) -> Bill | None:
        query = select(Bill).where(func.upper(Bill.code) == code.upper())
        async with self._sessions() as session:
            bill = (await session.execute(query)).scalar_one_or_none()
        return bill if bill is not None and bill.status == BillStatus.OPEN else None

    # the payer's latest open bill from the last few days, for claims sent without a code
    async def latest_paid_by(self, user_id: uuid.UUID, since: datetime) -> Bill | None:
        query = (
            select(Bill)
            .where(
                Bill.payer_id == user_id, Bill.status == BillStatus.OPEN, Bill.created_at >= since
            )
            .order_by(Bill.created_at.desc())
            .limit(1)
        )
        async with self._sessions() as session:
            return (await session.execute(query)).scalar_one_or_none()

    # the user's person on a bill, linking a guest of the same name or adding one
    async def person_for(self, bill_id: uuid.UUID, user_id: uuid.UUID, name: str) -> uuid.UUID:
        async with self._sessions() as session:
            mine = (await session.execute(
                select(Participant.id).where(
                    Participant.bill_id == bill_id, Participant.user_id == user_id
                )
            )).scalar_one_or_none()
            if mine is not None:
                return mine
            guest = (await session.execute(
                select(Participant).where(
                    Participant.bill_id == bill_id,
                    Participant.user_id.is_(None),
                    func.lower(Participant.guest_name) == name.lower()
                ).limit(1)
            )).scalar_one_or_none()
            if guest is None:
                guest = Participant(id=uuid.uuid4(), bill_id=bill_id, guest_name=name)
                session.add(guest)
            guest.user_id = user_id
            await session.flush()
            # a friend who links after the bill closed gets their rows now
            bill = await session.get(Bill, bill_id)
            if bill is not None and bill.status != BillStatus.OPEN:
                await post_late(session, bill, guest)
            await session.commit()
            return guest.id

    # save claims as given under the bill lock, replacing the same person and item
    async def apply(
        self, bill_id: uuid.UUID, claims: list[tuple[uuid.UUID, uuid.UUID, float]]
    ) -> bool:
        async with self._sessions() as session:
            bill = (await session.execute(
                select(Bill).where(Bill.id == bill_id).with_for_update()
            )).scalar_one()
            if bill.status != BillStatus.OPEN:
                return False
            for person, item, weight in claims:
                await session.execute(delete(ItemClaim).where(
                    ItemClaim.participant_id == person, ItemClaim.bill_item_id == item
                ))
                session.add(ItemClaim(participant_id=person, bill_item_id=item, weight=weight))
            await session.commit()
        return True

    # shares if the given claims were added, by person id, new names get stand in ids
    async def preview(
        self, bill: Bill, extra: list[tuple[uuid.UUID, uuid.UUID, float]]
    ) -> dict[uuid.UUID, int]:
        async with self._sessions() as session:
            items = list((await session.execute(
                select(BillItem).where(BillItem.bill_id == bill.id)
            )).scalars())
            claims = list((await session.execute(
                select(ItemClaim).where(ItemClaim.bill_item_id.in_([item.id for item in items]))
            )).scalars())
        pairs = {(person, item) for person, item, _ in extra}
        kept = [c for c in claims if (c.participant_id, c.bill_item_id) not in pairs]
        added = [
            ItemClaim(participant_id=person, bill_item_id=item, weight=weight)
            for person, item, weight in extra
        ]
        people = {person for person, _, _ in extra}
        return {person: preview_share(bill, items, [*kept, *added], person) for person in people}
