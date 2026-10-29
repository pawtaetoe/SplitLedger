import logging
import secrets
import uuid
from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from splitledger.contracts.messages import Inbound
from splitledger.contracts.transport import Transport
from splitledger.db.models import Bill, BillItem, BillStatus, ItemClaim, Participant, User
from splitledger.handlers.dashboard import LINK_MINUTES, SEE_ALL, dashboard_url
from splitledger.handlers.log import ExpenseLogger, ensure_user, money, receipt_adds_up
from splitledger.handlers.settle import OWE_ALL, SettleUp
from splitledger.llm.schemas import Locale, ReceiptDraft
from splitledger.split.edit import BillEditor
from splitledger.split.finalise import BillCloser, Closing, Leftovers
from splitledger.split.tokens import ClaimTokens, LoginLinks

LOG = logging.getLogger(__name__)
# days a bill can still be closed in words without its button
CLOSE_DAYS = 30
# no 0 O 1 I so a code read aloud or typed can't be mistaken
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6
# fresh codes to try before giving up on a clash
CODE_TRIES = 5
PAYER_NAME = "Payer"
MAX_WAYS = 50
# lines with more than this many are kept whole, like 40 dumplings
MAX_UNITS = 12
# hours a just made bill can still be switched to an even split by message
RECENT_HOURS = 24
NO_ITEMS = (
    "*I couldn't read the items*\n"
    "Splitting needs each line. Try a flat, well lit photo of the whole receipt."
)
READY = (
    "*Bill ready · code {}*\n"
    "Forward the next message to your group. Each friend replies with what they had.\n"
    "It closes by itself once everyone's in, and everyone is told what they owe.\n"
    "A line misread? Tap Fix the items before you forward it."
)
UPDATED = (
    "*Bill updated · code {}*\n"
    "Forward the next message to your group. It replaces any earlier one.\n"
    "It closes by itself once everyone's in."
)
FIX_FIRST = (
    "*The lines don't add up*\n"
    "With service and tax they come to {}, but the total is {}. Tap Fix the items to correct them, "
    "then I'll make the message for your friends."
)
FIX_LINK = (
    "*Fix the items*\n"
    "Change names or prices, add or remove lines, then tap Save. "
    "The button works for an hour."
)
EDIT_MINUTES = 60
READY_EVEN = (
    "*Split {} ways · code {}*\n"
    "Each friend pays *{}* and your share is *{}*.\n"
    "Forward the next message to your group. It closes by itself once everyone has joined."
)
UNKNOWN = "*I can't find that bill*\nSend a new receipt photo with the caption _split_."
NOT_PAYER = "*Only the person who paid can change this bill*"
HOW = "*How should I split {}?*\n{}"
HOW_ITEMS = "By what each had lets friends tick their own dishes."
HOW_EVEN_ONLY = "The lines don't add up. Fix the items to split by dish, or split it evenly."
HOW_MANY = "*How many people, counting you?*\nMore than 8? Reply like _split 12 ways_."
# head counts offered as buttons
HEADS = range(2, 9)
CLOSED_ALREADY = "*This bill is closed already*"
NOTHING_OPEN = (
    "*No open bill to close*\nSplit one first: send a receipt photo with the caption _split_."
)
LEFTOVER_ASK = "*Nobody claimed {}*\nWho pays for {}?"
SHORT_ASK = "*Only {} of {} people have joined*\nClose anyway? You cover the rest."
OWED_TO_FRIEND = "*You owe {} {}*\nFor {}. Logged under {} in your spending."
AUTO_CLOSED = "Everyone has claimed, so I closed it."
ALL_IN = (
    "*Everyone's in · {}*\n"
    "I'll close it in 15 minutes unless someone changes a claim, "
    "or tap Close the bill to close it now."
)
# minutes with no claim changes before a fully claimed bill closes itself
QUIET_MINUTES = 15
STILL_OPEN = (
    "*{} is still open*\n{}\n"
    "Close it so each person's share is logged and shows what they owe."
)
# hours before an open bill that isn't fully claimed gets one reminder
NUDGE_HOURS = 24
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
def forward_text(bill: Bill, items: list[BillItem], chat_url: str) -> str:
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
        f"{title}\nTap the link, send the message, then reply with the numbers you had.\n\n"
        + "\n".join(lines) + extra
        + f"\n\n*Claim in WhatsApp*\n{chat_url}"
    )


# what each friend pays and what the payer covers, the payer takes the odd cents
def even_share(total: int, ways: int) -> tuple[int, int]:
    each = total // ways
    return each, total - each * (ways - 1)


# the message friends get for an even split
def even_text(bill: Bill, chat_url: str) -> str:
    ways = bill.even_ways or 1
    each, _ = even_share(bill.total, ways)
    where = f"{bill.vendor} · " if bill.vendor else "Bill · "
    return (
        f"*{where}{money(bill.total, bill.currency)}*\n"
        f"Split evenly {ways} ways: *{money(each, bill.currency)}* each.\n\n"
        f"*Say you're in, in WhatsApp*\n{chat_url}"
    )


# one row per unit so two of a dish can go to different people, odd cents to the first ones
def units(receipt: ReceiptDraft) -> list[tuple[str, int]]:
    return [
        row for item in receipt.items for row in unit_prices(item.name, item.qty, item.price_cents)
    ]


# one line split into one price per unit, kept whole when it is one or very many
def unit_prices(name: str, qty: int, cents: int) -> list[tuple[str, int]]:
    count = qty if 1 < qty <= MAX_UNITS else 1
    each, extra = divmod(cents, count)
    return [(name, each + (1 if n < extra else 0)) for n in range(count)]


# the button that closes a bill
def close_button(bill: Bill) -> list[tuple[str, str]]:
    return [(f"bill:close:{bill.code}", "Close the bill")]


# the fix the items button for the payer of a bill split by item
def edit_button(bill: Bill) -> tuple[str, str]:
    return f"bill:edit:{bill.code}", "Fix the items"


# links that let the payer edit one bill for an hour
def edit_links(tokens: ClaimTokens, code: str) -> LoginLinks:
    return LoginLinks(tokens, EDIT_MINUTES * 60, f"edit-{code.upper()}")


# turns a group receipt into a shared bill friends can claim from
class GroupBills:
    # sessions, the logger that reads receipts, signed links and where they point
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], logger: ExpenseLogger, locale: Locale,
        tokens: ClaimTokens, base_url: str, bot_number: str, settle: SettleUp | None = None
    ) -> None:
        self._sessions = sessions
        self._logger = logger
        self._locale = locale
        self._tokens = tokens
        self._base_url = base_url.rstrip("/")
        self._bot_number = bot_number
        self._closer = BillCloser(sessions)
        self._editor = BillEditor(sessions)
        self._me = LoginLinks(tokens, LINK_MINUTES * 60)
        self._settle = settle

    # read the photo, check it can be split, save the bill and send the forwardable message
    # unsure saves the bill and asks how to split it before anything goes to friends
    async def from_receipt(
        self, inbound: Inbound, transport: Transport, ways: int | None = None,
        unsure: bool = False
    ) -> Bill | None:
        if ways is not None and not 2 <= ways <= MAX_WAYS:
            await transport.send_text(inbound.user_id, BAD_WAYS)
            return None
        image = await transport.fetch_media(inbound.media_ref or "")
        receipt = await self._logger.read_receipt(image)
        # an even split only needs the total, a split by item needs lines that add up
        problem = NO_TOTAL if not receipt.total_cents else None
        by_item = bool(receipt.items) and receipt_adds_up(receipt)
        if unsure and problem is None:
            bill, _ = await self._create(inbound, receipt, None)
            await self._ask_how(inbound, transport, bill, ways, by_item)
            return None
        if ways is None and problem is None and receipt.items and not receipt_adds_up(receipt):
            bill, items = await self._create(inbound, receipt, None)
            charges = bill.service + bill.tax - bill.discount
            lines = money(sum(item.price_cents for item in items) + charges, bill.currency)
            text = FIX_FIRST.format(lines, money(bill.total, bill.currency))
            await transport.send_buttons(inbound.user_id, text, [edit_button(bill)])
            return None
        if ways is None and problem is None and not receipt.items:
            problem = NO_ITEMS
        if problem:
            await transport.send_text(inbound.user_id, problem)
            return None
        bill, items = await self._create(inbound, receipt, ways)
        await self._send(inbound.user_id, transport, bill, items)
        return bill

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
        await self._send(inbound.user_id, transport, bill, [])

    # the note for the payer then the message to forward, by item or even
    async def _send(
        self, to: str, transport: Transport, bill: Bill, items: list[BillItem], ready: str = READY
    ) -> None:
        chat_url = f"https://wa.me/{self._bot_number}?text={quote(f'CLAIM {bill.code}')}"
        if bill.even_ways:
            each, mine = even_share(bill.total, bill.even_ways)
            ready = READY_EVEN.format(
                bill.even_ways, bill.code, money(each, bill.currency), money(mine, bill.currency)
            )
            await transport.send_buttons(to, ready, close_button(bill))
            await transport.send_text(to, even_text(bill, chat_url))
            return
        buttons = [edit_button(bill), *close_button(bill)]
        await transport.send_buttons(to, ready.format(bill.code), buttons)
        await transport.send_text(to, forward_text(bill, items, chat_url))

    # after the payer fixed the items, a fresh message to forward instead of the first one
    async def edited(self, to: str, code: str, transport: Transport) -> None:
        async with self._sessions() as session:
            bill = (await session.execute(select(Bill).where(Bill.code == code))).scalar_one()
            items = list((await session.execute(
                select(BillItem).where(BillItem.bill_id == bill.id).order_by(BillItem.position)
            )).scalars())
        await self._send(to, transport, bill, items, UPDATED)

    # close by button: the first tap, then leftovers or a short even split answered
    async def on_button(self, inbound: Inbound, button_id: str, transport: Transport) -> None:
        if button_id.startswith("pay:"):
            if self._settle is not None:
                await self._settle.on_button(inbound, button_id, transport)
            return
        parts = button_id.split(":")
        if len(parts) < 3:
            return
        action, code, choice = parts[1], parts[2], parts[3] if len(parts) > 3 else "ask"
        async with self._sessions() as session:
            query = (
                select(Bill, User.wa_user_id)
                .join(User, Bill.payer_id == User.id)
                .where(Bill.code == code)
            )
            row = (await session.execute(query)).first()
        if row is None:
            await transport.send_text(inbound.user_id, UNKNOWN)
            return
        bill, payer_wa = row
        if payer_wa != inbound.user_id:
            await transport.send_text(inbound.user_id, NOT_PAYER)
            return
        if action in ("items", "even"):
            await self._chosen(inbound, transport, bill, action, choice)
            return
        if action == "edit":
            await self._edit_link(inbound, transport, bill)
            return
        await self._close(inbound, transport, bill, choice if action == "rest" else (
            "force" if action == "force" else "ask"
        ))

    # a link to fix the items, only while no friend has joined
    async def _edit_link(self, inbound: Inbound, transport: Transport, bill: Bill) -> None:
        found = await self._editor.load(bill.code, bill.payer_id)
        if isinstance(found, str):
            await transport.send_text(inbound.user_id, found)
            return
        token = edit_links(self._tokens, bill.code).make(str(bill.payer_id), datetime.now(UTC))
        url = f"{self._base_url}/e/{bill.code}/{token}"
        await transport.send_link(inbound.user_id, FIX_LINK, "Fix the items", url)

    # how to split, by item only when the lines add up, evenly with the head count if known
    async def _ask_how(
        self, inbound: Inbound, transport: Transport, bill: Bill, ways: int | None,
        by_item: bool
    ) -> None:
        buttons = [(f"bill:items:{bill.code}", "By what each had")] if by_item else [
            edit_button(bill)
        ]
        even = f"Evenly, {ways} ways" if ways else "Evenly"
        buttons.append((f"bill:even:{bill.code}:{ways or 0}", even))
        text = HOW.format(money(bill.total, bill.currency), HOW_ITEMS if by_item else HOW_EVEN_ONLY)
        await transport.send_buttons(inbound.user_id, text, buttons)

    # the payer's answer to how to split, then the usual messages to forward
    async def _chosen(
        self, inbound: Inbound, transport: Transport, bill: Bill, action: str, choice: str
    ) -> None:
        if bill.status != BillStatus.OPEN:
            await transport.send_text(inbound.user_id, CLOSED_ALREADY)
            return
        if action == "even" and not (choice.isdigit() and 2 <= int(choice) <= MAX_WAYS):
            buttons = [(f"bill:even:{bill.code}:{n}", f"{n} people") for n in HEADS]
            await transport.send_buttons(inbound.user_id, HOW_MANY, buttons)
            return
        async with self._sessions() as session:
            fresh = await session.get(Bill, bill.id)
            if fresh is None:
                return
            fresh.even_ways = int(choice) if action == "even" else None
            items = list((await session.execute(
                select(BillItem).where(BillItem.bill_id == bill.id).order_by(BillItem.position)
            )).scalars())
            await session.commit()
        await self._send(inbound.user_id, transport, fresh, items)

    # close bills everyone has claimed, and ask once about bills left open a day
    async def nudge(self, transport: Transport, now: datetime) -> int:
        query = (
            select(Bill, User.wa_user_id)
            .join(User, Bill.payer_id == User.id)
            .where(Bill.status == BillStatus.OPEN)
        )
        ready: list[tuple[Bill, str]] = []
        sent = 0
        async with self._sessions() as session:
            rows = (await session.execute(query)).all()
            for bill, payer_wa in rows:
                done, text = await self._state(session, bill, now)
                if done:
                    ready.append((bill, payer_wa))
                    continue
                if text is None or bill.nudged_at is not None:
                    continue
                try:
                    await transport.send_buttons(payer_wa, text, close_button(bill))
                except Exception as error:
                    LOG.warning("close nudge failed with %s", type(error).__name__)
                    continue
                bill.nudged_at = now
                sent += 1
            await session.commit()
        for bill, payer_wa in ready:
            closing = await self._closer.close(bill.id)
            if closing is not None:
                await self._tell(payer_wa, transport, closing, AUTO_CLOSED)
                sent += 1
        return sent

    # whether to close now, and what to tell the payer: everyone's in, or left open a day
    async def _state(
        self, session: AsyncSession, bill: Bill, now: datetime
    ) -> tuple[bool, str | None]:
        people = (await session.execute(
            select(func.count()).where(Participant.bill_id == bill.id)
        )).scalar_one()
        items = select(BillItem.id).where(BillItem.bill_id == bill.id)
        count = select(func.count()).select_from(items.subquery())
        total = (await session.execute(count)).scalar_one()
        claimed = (await session.execute(
            select(func.count(func.distinct(ItemClaim.bill_item_id)))
            .where(ItemClaim.bill_item_id.in_(items))
        )).scalar_one()
        everyone = (
            people >= bill.even_ways if bill.even_ways
            else bool(total) and claimed == total and people > 1
        )
        quiet = bill.claimed_at is None or now - bill.claimed_at >= timedelta(minutes=QUIET_MINUTES)
        if everyone:
            return quiet, None if quiet else ALL_IN.format(bill.vendor or "Your bill")
        if now - bill.created_at < timedelta(hours=NUDGE_HOURS) or people < 2:
            return False, None
        left = (
            f"{people} of {bill.even_ways} people have joined." if bill.even_ways
            else f"{total - claimed} of {total} items are still unclaimed."
        )
        return False, STILL_OPEN.format(bill.vendor or "Your bill", left)

    # close from the web page: only the payer, leftovers come back as names to ask about
    async def close_for(
        self, user_id: uuid.UUID, code: str, choice: str, transport: Transport
    ) -> tuple[str, list[str]]:
        query = (
            select(Bill, User.wa_user_id)
            .join(User, Bill.payer_id == User.id)
            .where(Bill.code == code)
        )
        async with self._sessions() as session:
            row = (await session.execute(query)).first()
        if row is None or row[0].payer_id != user_id:
            return "not_payer", []
        bill, payer_wa = row
        try:
            closing = await self._closer.close(
                bill.id, choice if choice in ("even", "mine") else "ask"
            )
        except Leftovers as left:
            return "leftovers", left.names
        if closing is None:
            return "closed_already", []
        await self._tell(payer_wa, transport, closing)
        return "closed", []

    # close the payer's latest open bill, asked for in words
    async def close_latest(self, inbound: Inbound, transport: Transport) -> None:
        since = datetime.now(UTC) - timedelta(days=CLOSE_DAYS)
        query = (
            select(Bill)
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
            bill = (await session.execute(query)).scalar_one_or_none()
        if bill is None:
            await transport.send_text(inbound.user_id, NOTHING_OPEN)
            return
        await self._close(inbound, transport, bill, "ask")

    # ask about anything unsure, else close once and tell everyone their share
    async def _close(self, inbound: Inbound, transport: Transport, bill: Bill, choice: str) -> None:
        if bill.status != BillStatus.OPEN:
            await transport.send_text(inbound.user_id, CLOSED_ALREADY)
            return
        if bill.even_ways and choice == "ask":
            async with self._sessions() as session:
                joined = (await session.execute(
                    select(func.count()).where(Participant.bill_id == bill.id)
                )).scalar_one()
            if joined < bill.even_ways:
                await transport.send_buttons(
                    inbound.user_id, SHORT_ASK.format(joined, bill.even_ways),
                    [(f"bill:force:{bill.code}", "Close anyway")]
                )
                return
        try:
            closing = await self._closer.close(
                bill.id, choice if choice in ("even", "mine") else "ask"
            )
        except Leftovers as left:
            names = ", ".join(left.names)
            them = "it" if len(left.names) == 1 else "them"
            await transport.send_buttons(
                inbound.user_id, LEFTOVER_ASK.format(names, them),
                [
                    (f"bill:rest:{bill.code}:even", "Everyone shares"),
                    (f"bill:rest:{bill.code}:mine", "I'll pay")
                ]
            )
            return
        if closing is None:
            await transport.send_text(inbound.user_id, CLOSED_ALREADY)
            return
        await self._tell(inbound.user_id, transport, closing)

    # the payer gets everyone's share, each friend with an account gets what they owe
    async def _tell(
        self, payer_wa: str, transport: Transport, closing: Closing, why: str = ""
    ) -> None:
        bill = closing.bill
        c = bill.currency
        category = bill.category or "Other"
        mine = next(share.cents for share in closing.shares if share.person.id == closing.payer.id)
        owed = [
            f"- {share.person.guest_name} *{money(share.cents, c)}*"
            for share in closing.shares if share.person.id != closing.payer.id and share.cents
        ]
        text = (
            f"*Bill closed · {bill.vendor or 'Bill'}*\n" + (f"{why}\n" if why else "")
            + f"You paid {money(bill.total, c)}. "
            f"Your share *{money(mine, c)}* is logged under {category}."
        )
        if owed:
            text += "\n\n*Owed to you*\n" + "\n".join(owed)
        me = dashboard_url(self._base_url, self._me, bill.payer_id)
        await transport.send_link(payer_wa, text, SEE_ALL, me)
        friends = [s.person.user_id for s in closing.shares if s.person.user_id and s.cents]
        async with self._sessions() as session:
            users = {
                user.id: user for user in (await session.execute(
                    select(User).where(User.id.in_(friends))
                )).scalars()
            }
        what = bill.vendor or "a shared bill"
        payer = users.get(bill.payer_id)
        for share in closing.shares:
            user = users.get(share.person.user_id) if share.person.user_id else None
            if user is None or share.person.id == closing.payer.id or not share.cents:
                continue
            amount = money(share.cents, c)
            text = OWED_TO_FRIEND.format(closing.payer.guest_name, amount, what, category)
            try:
                if self._settle is not None and payer is not None:
                    await self._settle.ask(
                        transport, user, payer, c, text, share.cents, f"SplitLedger {bill.code}"
                    )
                else:
                    await transport.send_buttons(user.wa_user_id, text, [OWE_ALL])
            except Exception as error:
                LOG.warning("owed message failed with %s", type(error).__name__)

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
                    currency=receipt.currency or (
                        payer.currency if payer is not None else self._locale.currency
                    ),
                    category=receipt.category, even_ways=ways,
                    subtotal=sum(item.price_cents for item in receipt.items),
                    service=receipt.service_cents, tax=receipt.tax_cents,
                    discount=receipt.discount_cents, total=receipt.total_cents or 0,
                    status=BillStatus.OPEN
                )
                items = [
                    BillItem(
                        bill_id=bill.id, name=name, qty=1, price_cents=cents, position=position
                    )
                    for position, (name, cents) in enumerate(units(receipt))
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
