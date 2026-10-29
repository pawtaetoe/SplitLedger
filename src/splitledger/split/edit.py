import re
import uuid
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from splitledger.db.models import Bill, BillItem, BillStatus, ItemClaim, Participant

# lines a bill can have after editing
MAX_LINES = 60
# a price like 15, 15.5, 15.50 or 15,50
PRICE = re.compile(r"(\d{1,7})(?:[.,](\d{1,2}))?")
NOT_YOURS = "Only the person who paid can fix this bill."
CLOSED = "This bill is closed, so it can't change now."
STARTED = "Friends have started claiming, so the items can't change now."
NO_LINES = "Keep at least one line with a name and a price."
TOO_MANY = f"A bill can have up to {MAX_LINES} lines."


# one row from the form, no id for a new line
@dataclass(frozen=True)
class Row:
    item: str
    name: str
    price: str


# cents from what was typed, none when it isn't a price
def cents_of(text: str) -> int | None:
    found = PRICE.fullmatch(text.strip().removeprefix("$"))
    if found is None:
        return None
    whole, part = found.group(1), (found.group(2) or "0").ljust(2, "0")
    return int(whole) * 100 + int(part)


# whether the text is an item id from the form
def is_id(text: str) -> bool:
    try:
        uuid.UUID(text)
    except ValueError:
        return False
    return True


# the payer fixes a misread receipt while nobody else has joined
class BillEditor:
    # sessions for bills and their items
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    # the bill and its lines when this user may edit it, else why not
    async def load(self, code: str, user_id: uuid.UUID) -> tuple[Bill, list[BillItem]] | str:
        async with self._sessions() as session:
            found = await self._check(session, code, user_id)
            if isinstance(found, str):
                return found
            items = list((await session.execute(
                select(BillItem).where(BillItem.bill_id == found.id).order_by(BillItem.position)
            )).scalars())
        return found, items

    # replace the lines, the vendor and the extra charges, the total follows, none when saved
    async def save(
        self, code: str, user_id: uuid.UUID, vendor: str, rows: list[Row], charges: str
    ) -> str | None:
        kept = [row for row in rows if row.name.strip()]
        if any(row.item and not is_id(row.item) for row in kept):
            return NO_LINES
        prices = [cents_of(row.price) for row in kept]
        extra = cents_of(charges) if charges.strip() else 0
        if not kept or any(price is None for price in prices) or extra is None:
            return NO_LINES
        if len(kept) > MAX_LINES:
            return TOO_MANY
        async with self._sessions() as session:
            bill = await self._check(session, code, user_id, lock=True)
            if isinstance(bill, str):
                return bill
            items = {item.id: item for item in (await session.execute(
                select(BillItem).where(BillItem.bill_id == bill.id)
            )).scalars()}
            seen: set[uuid.UUID] = set()
            for position, (row, price) in enumerate(zip(kept, prices, strict=True)):
                item = items.get(uuid.UUID(row.item)) if row.item else None
                if item is None:
                    item = BillItem(bill_id=bill.id, name="", price_cents=0)
                    session.add(item)
                else:
                    seen.add(item.id)
                item.name, item.qty, item.price_cents = row.name.strip()[:60], 1, price or 0
                item.position = position
            gone = [item_id for item_id in items if item_id not in seen]
            if gone:
                await session.execute(delete(ItemClaim).where(ItemClaim.bill_item_id.in_(gone)))
                await session.execute(delete(BillItem).where(BillItem.id.in_(gone)))
            bill.vendor = vendor.strip()[:60] or None
            bill.subtotal = sum(price or 0 for price in prices)
            bill.service, bill.tax, bill.discount = extra, 0, 0
            bill.total = bill.subtotal + extra
            await session.commit()
        return None

    # the open bill when this user paid it and nobody else has joined, else why not
    async def _check(
        self, session: AsyncSession, code: str, user_id: uuid.UUID, lock: bool = False
    ) -> Bill | str:
        query = select(Bill).where(Bill.code == code.upper())
        if lock:
            query = query.with_for_update()
        bill = (await session.execute(query)).scalar_one_or_none()
        if bill is None or bill.payer_id != user_id:
            return NOT_YOURS
        if bill.status != BillStatus.OPEN:
            return CLOSED
        others = (await session.execute(
            select(func.count()).where(
                Participant.bill_id == bill.id, Participant.user_id.is_not(None),
                Participant.user_id != user_id
            )
        )).scalar_one()
        return STARTED if others else bill
