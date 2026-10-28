import secrets
import uuid
from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from splitledger.contracts.messages import Inbound
from splitledger.contracts.transport import Transport
from splitledger.db.models import Bill, BillItem, BillStatus, Participant, User
from splitledger.handlers.log import ExpenseLogger, ensure_user, money, receipt_adds_up
from splitledger.llm.schemas import Locale, ReceiptDraft
from splitledger.split.tokens import ClaimTokens

# no 0 O 1 I so a code read aloud or typed can't be mistaken
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6
# fresh codes to try before giving up on a clash
CODE_TRIES = 5
PAYER_NAME = "Payer"
MAX_WAYS = 50
# hours a just made bill can still be switched to an even split by message
RECENT_HOURS = 24
NO_ITEMS = (
    "*I couldn't read the items*\n"
    "Splitting needs each line. Try a flat, well lit photo of the whole receipt."
)
NOT_ADDING_UP = (
    "*The lines don't add up*\n"
    "I can't split this one by item. Try a clearer photo, or log it with no caption."
)
READY = (
    "*Bill ready · code {}*\n"
    "Forward the next message to your group. Each friend taps what they had."
)
READY_EVEN = (
    "*Split {} ways · code {}*\n"
    "Each friend pays *{}* and your share is *{}*.\n"
    "Forward the next message to your group."
)
NO_TOTAL = "*I couldn't read the total*\nTry a flat, well lit photo of the whole receipt."
NO_BILL = (
    "*Which bill?*\nSend the receipt photo with the caption _split {} ways_ "
    "and I'll share it evenly."
)
BAD_WAYS = f"*How many people?*\nSay a number from 2 to {MAX_WAYS}, like _split 4 ways_."


# a random bill code that is not in taken
def new_code(taken: Collection[str] = ()) -> str:
    while True:
        code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))
        if code not in taken:
            return code


# the message friends get, items numbered so they can claim by number too
def forward_text(bill: Bill, items: list[BillItem], claim_url: str, chat_url: str) -> str:
    c = bill.currency
    title = f"*{bill.vendor} · {money(bill.total, c)}*" if bill.vendor else (
        f"*Bill · {money(bill.total, c)}*"
    )
    lines = [
        f"{n}. {item.name}{f' x{item.qty}' if item.qty > 1 else ''} · {money(item.price_cents, c)}"
        for n, item in enumerate(items, 1)
    ]
    charges = bill.service + bill.tax - bill.discount
    extra = f"\nService, tax and discounts: {money(charges, c)}" if charges else ""
    return (
        f"{title}\nTap what you had and I'll work out your share.\n\n"
        + "\n".join(lines) + extra
        + f"\n\n*Claim on the web*\n{claim_url}\n\n*Or in WhatsApp*\n{chat_url}"
    )


# what each friend pays and what the payer covers, the payer takes the odd cents
def even_share(total: int, ways: int) -> tuple[int, int]:
    each = total // ways
    return each, total - each * (ways - 1)


# the message friends get for an even split
def even_text(bill: Bill, claim_url: str, chat_url: str) -> str:
    ways = bill.even_ways or 1
    each, _ = even_share(bill.total, ways)
    where = f"{bill.vendor} · " if bill.vendor else "Bill · "
    return (
        f"*{where}{money(bill.total, bill.currency)}*\n"
        f"Split evenly {ways} ways: *{money(each, bill.currency)}* each.\n\n"
        f"*Say you're in on the web*\n{claim_url}\n\n*Or in WhatsApp*\n{chat_url}"
    )


# turns a group receipt into a shared bill friends can claim from
class GroupBills:
    # sessions, the logger that reads receipts, signed links and where they point
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], logger: ExpenseLogger, locale: Locale,
        tokens: ClaimTokens, base_url: str, bot_number: str
    ) -> None:
        self._sessions = sessions
        self._logger = logger
        self._locale = locale
        self._tokens = tokens
        self._base_url = base_url.rstrip("/")
        self._bot_number = bot_number

    # read the photo, check it can be split, save the bill and send the forwardable message
    async def from_receipt(
        self, inbound: Inbound, transport: Transport, ways: int | None = None
    ) -> None:
        if ways is not None and not 2 <= ways <= MAX_WAYS:
            await transport.send_text(inbound.user_id, BAD_WAYS)
            return
        image = await transport.fetch_media(inbound.media_ref or "")
        receipt = await self._logger.read_receipt(image)
        # an even split only needs the total, a split by item needs lines that add up
        problem = NO_TOTAL if not receipt.total_cents else None
        if ways is None and problem is None:
            problem = NO_ITEMS if not receipt.items else (
                None if receipt_adds_up(receipt) else NOT_ADDING_UP
            )
        if problem:
            await transport.send_text(inbound.user_id, problem)
            return
        bill, items = await self._create(inbound, receipt, ways)
        await self._send(inbound, transport, bill, items)

    # share the payer's latest open bill evenly, asked for by message after the photo
    async def split_evenly(self, inbound: Inbound, transport: Transport, ways: int) -> None:
        if not 2 <= ways <= MAX_WAYS:
            await transport.send_text(inbound.user_id, BAD_WAYS)
            return
        since = datetime.now(UTC) - timedelta(hours=RECENT_HOURS)
        latest = (
            select(Bill.id)
            .join(User, Bill.payer_id == User.id)
            .where(
                User.wa_user_id == inbound.user_id,
                Bill.status == BillStatus.OPEN,
                Bill.created_at >= since
            )
            .order_by(Bill.created_at.desc())
            .limit(1)
        )
        async with self._sessions() as session:
            bill_id = (await session.execute(latest)).scalar_one_or_none()
            bill = await session.get(Bill, bill_id) if bill_id else None
            if bill is not None:
                bill.even_ways = ways
                await session.commit()
        if bill is None:
            await transport.send_text(inbound.user_id, NO_BILL.format(ways))
            return
        await self._send(inbound, transport, bill, [])

    # the note for the payer then the message to forward, by item or even
    async def _send(
        self, inbound: Inbound, transport: Transport, bill: Bill, items: list[BillItem]
    ) -> None:
        claim_url = f"{self._base_url}/c/{self._tokens.make(bill.code, datetime.now(UTC))}"
        chat_url = f"https://wa.me/{self._bot_number}?text={quote(f'CLAIM {bill.code}')}"
        if bill.even_ways:
            each, mine = even_share(bill.total, bill.even_ways)
            ready = READY_EVEN.format(
                bill.even_ways, bill.code, money(each, bill.currency), money(mine, bill.currency)
            )
            await transport.send_text(inbound.user_id, ready)
            await transport.send_text(inbound.user_id, even_text(bill, claim_url, chat_url))
            return
        await transport.send_text(inbound.user_id, READY.format(bill.code))
        await transport.send_text(inbound.user_id, forward_text(bill, items, claim_url, chat_url))

    # save the bill, its items and the payer as the first participant, retrying a code clash
    async def _create(
        self, inbound: Inbound, receipt: ReceiptDraft, ways: int | None
    ) -> tuple[Bill, list[BillItem]]:
        for _ in range(CODE_TRIES):
            async with self._sessions() as session:
                user_id, _ = await ensure_user(session, inbound.user_id, self._locale)
                payer = await session.get(User, user_id)
                if payer is not None and not payer.display_name and inbound.name:
                    payer.display_name = inbound.name
                bill = Bill(
                    id=uuid.uuid4(), code=new_code(), payer_id=user_id, vendor=receipt.vendor,
                    currency=receipt.currency or self._locale.currency,
                    category=receipt.category, even_ways=ways,
                    subtotal=sum(item.price_cents for item in receipt.items),
                    service=receipt.service_cents, tax=receipt.tax_cents,
                    discount=receipt.discount_cents, total=receipt.total_cents or 0,
                    status=BillStatus.OPEN
                )
                items = [
                    BillItem(
                        bill_id=bill.id, name=item.name, qty=item.qty,
                        price_cents=item.price_cents, position=position
                    )
                    for position, item in enumerate(receipt.items)
                ]
                name = (payer.display_name if payer else None) or PAYER_NAME
                payer_row = Participant(bill_id=bill.id, user_id=user_id, guest_name=name)
                try:
                    session.add(bill)
                    await session.flush()
                    session.add_all([*items, payer_row])
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    continue
            return bill, items
        raise RuntimeError("no free bill code")
