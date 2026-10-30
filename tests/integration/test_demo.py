from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from splitledger.contracts.messages import Inbound
from splitledger.db.models import Bill, BillStatus, LedgerEntry, LedgerKind, User
from splitledger.handlers.ask import SpendAssistant
from splitledger.handlers.bill import GroupBills
from splitledger.handlers.bills import UpcomingBills
from splitledger.handlers.claim import ClaimChat
from splitledger.handlers.currency import CurrencyChoice
from splitledger.handlers.dashboard import DashboardLink
from splitledger.handlers.log import ExpenseLogger
from splitledger.handlers.plan import PlanManager
from splitledger.handlers.router import PROMPT, MessageReader, Router
from splitledger.handlers.settle import SettleUp, paid_button
from splitledger.llm.schemas import ExpenseDraft, MessageResult, ReceiptDraft, ReceiptItem
from splitledger.llm.schemas_sg import SG
from splitledger.pay.rails import PayRails
from splitledger.pay.rails_sg import PayNow
from splitledger.plans.afford import CORE_RULES, AffordPack
from splitledger.plans.quota import QuotaBook
from splitledger.split.claims import ClaimBook
from splitledger.split.overview import Overview
from splitledger.split.tokens import ClaimTokens, LoginLinks
from tests.fakes import FakeLLM, FakeTransport

TOMMY, ANNA, BEN = "6581234567", "6591111111", "6592222222"
TOKENS = ClaimTokens("secret")
DINNER = ReceiptDraft(
    vendor="Jumbo", currency="SGD", category="Dining",
    items=[
        ReceiptItem(name="Chilli crab", price_cents=6000),
        ReceiptItem(name="Beer", qty=2, price_cents=2000),
        ReceiptItem(name="Rice", price_cents=1000)
    ],
    subtotal_cents=9000, total_cents=9000
)


# the model's reading of each message the demo sends
def readings() -> dict[str, object]:
    said = {
        "kopi 2.20": MessageResult(
            intent="log", expense=ExpenseDraft(amount_cents=220, category="Dining", note="kopi")
        ),
        "split": MessageResult(intent="split", split_mode="items")
    }
    return {f"read_message: {PROMPT}{text}": result for text, result in said.items()} | {
        "parse_receipt": DINNER
    }


# the whole app wired as in production, on the test database and fakes
def app(sessions) -> tuple[Router, GroupBills, Overview]:
    llm = FakeLLM(readings())
    book = QuotaBook(sessions)
    logger = ExpenseLogger(llm, sessions, SG, book)
    links = DashboardLink(sessions, LoginLinks(TOKENS, 900), "https://sl.test", SG)
    settle = SettleUp(sessions, PayRails((PayNow(),)))
    group = GroupBills(sessions, logger, SG, TOKENS, "https://sl.test", "6512345678", settle)
    assistant = SpendAssistant(llm, sessions, book, AffordPack(CORE_RULES))
    router = Router(
        logger, MessageReader(llm, SG), assistant,
        PlanManager(sessions, book, SG, llm), UpcomingBills(sessions, logger, 9), group,
        ClaimChat(sessions, llm, ClaimBook(sessions), SG, links), links, CurrencyChoice(sessions)
    )
    return router, group, Overview(sessions, book)


# one text from a person with their whatsapp name
def text(user: str, body: str, name: str) -> Inbound:
    return Inbound(f"m-{user}-{body}", user, "text", text=body, name=name)


# the newest reply to one person, text or buttons
def last(transport: FakeTransport, user: str) -> str:
    return next((body for to, body in reversed(transport.sent) if to == user), "")


# log and undo, split a receipt, two friends claim from the chat, it closes itself, everyone sees
# what they owe
async def test_demo(sessions):
    router, group, overview = app(sessions)
    transport = FakeTransport()

    # 1 and 2, a quick log with undo
    await router.handle(text(TOMMY, "kopi 2.20", "Tommy"), transport)
    logged = transport.sent_buttons[-1]
    assert logged[1].startswith("✅ *Logged SGD 2.20*\nDining · kopi")
    undo = logged[2][0][0]
    await router.handle(Inbound("m-undo", TOMMY, "button", button_id=undo), transport)
    assert transport.sent_text[-1] == (TOMMY, "*Removed SGD 2.20 · Dining · kopi*")

    # 3, the receipt with the caption split becomes a bill to forward
    transport.media["photo"] = b"jpeg"
    photo = Inbound("m-photo", TOMMY, "image", text="split", media_ref="photo", name="Tommy")
    await router.handle(photo, transport)
    async with sessions() as session:
        bill = (await session.execute(select(Bill))).scalar_one()
    assert transport.sent_buttons[-1][1].startswith(f"*Bill ready · code {bill.code}*")
    assert f"CLAIM%20{bill.code}" in transport.sent_text[-1][1]

    # 4, each person claims in the chat, friends skip the welcome and reply with numbers
    await router.handle(text(TOMMY, f"CLAIM {bill.code} 1", "Tommy"), transport)
    await router.handle(text(ANNA, f"CLAIM {bill.code}", "Anna"), transport)
    assert "Reply with the numbers you had" in last(transport, ANNA)
    assert not any(to == ANNA and body.startswith("*Hi!") for to, body, _ in transport.sent_buttons)
    await router.handle(text(ANNA, "2", "Anna"), transport)
    assert last(transport, ANNA).startswith("*Your share so far · SGD 10.00*")
    assert last(transport, ANNA).endswith("I'll message you when the bill closes.")
    await router.handle(text(BEN, f"CLAIM {bill.code} 3, 4", "Ben"), transport)
    assert last(transport, BEN).startswith("*Your share so far · SGD 20.00*")

    # 5, everyone is in, the payer may close now, after 15 quiet minutes it closes itself
    now = datetime.now(UTC)
    assert await group.nudge(transport, now) == 1
    assert last(transport, TOMMY).startswith("*Everyone's in · Jumbo*")
    assert await group.nudge(transport, now + timedelta(minutes=16)) == 1
    closed = last(transport, TOMMY)
    assert "- Anna *SGD 10.00*" in closed and "- Ben *SGD 20.00*" in closed
    to, _, label, url = transport.sent_links[-1]
    assert (to, label) == (TOMMY, "My dashboard") and url.startswith("https://sl.test/d/")
    assert last(transport, ANNA).startswith("*You owe Tommy SGD 10.00*")
    assert "*PayNow* to 8123 4567" in last(transport, ANNA)
    assert any(to == ANNA for to, _, _ in transport.sent_images)

    # 6, the button under you owe lists everything without the model
    await router.handle(Inbound("m-owe", ANNA, "button", button_id="owe:all"), transport)
    assert last(transport, ANNA) == "*You owe*\n- Tommy *SGD 10.00*"

    # 6b, anna pays by paynow and taps i've paid, tommy is told
    async with sessions() as session:
        tommy = (await session.execute(select(User).where(User.wa_user_id == TOMMY))).scalar_one()
    done = paid_button(tommy.id, "SGD")[0]
    await router.handle(Inbound("m-paid", ANNA, "button", button_id=done), transport)
    assert last(transport, TOMMY).startswith("*Anna paid you SGD 10.00*")

    # 7, the ledger and the dashboard agree
    async with sessions() as session:
        stored = await session.get(Bill, bill.id)
        tommy = (await session.execute(select(User).where(User.wa_user_id == TOMMY))).scalar_one()
        rows = (await session.execute(
            select(LedgerEntry.kind, LedgerEntry.amount_cents)
            .where(LedgerEntry.user_id == tommy.id, LedgerEntry.voided_at.is_(None))
        )).all()
    assert stored is not None and stored.status == BillStatus.FINALISED
    assert sorted(rows) == sorted([
        (LedgerKind.EXPENSE, 6000), (LedgerKind.RECEIVABLE, 1000), (LedgerKind.RECEIVABLE, 2000),
        (LedgerKind.SETTLEMENT, 1000)
    ])
    board = await overview.build(tommy.id)
    assert board is not None
    assert [(b.who, b.cents) for b in board.people()] == [("Ben", 2000)]
    assert board.month == [("Dining", 6000)]
    assert ZoneInfo(board.user.timezone).key == "Asia/Singapore"
