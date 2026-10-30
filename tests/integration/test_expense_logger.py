from sqlalchemy import func, select

from splitledger.contracts.messages import Inbound
from splitledger.db.models import LedgerEntry, User
from splitledger.handlers import log as log_module
from splitledger.handlers.log import READING, SPLIT_GONE, ExpenseLogger, quick_parse
from splitledger.llm.schemas import GLOBAL, ExpenseDraft, ReceiptDraft, ReceiptItem
from tests.fakes import FakeLLM, FakeTransport
from tests.fakes.router import fake_router


# one text message from a user
def text(user: str, body: str) -> Inbound:
    return Inbound(f"m-{user}-{body}", user, "text", text=body)


# log one message with the draft the rules read from it
async def log(logger: ExpenseLogger, transport: FakeTransport, user: str, body: str) -> None:
    draft = quick_parse(body, GLOBAL)
    assert draft is not None
    await logger.log(text(user, body), transport, draft)


# count rows of one model
async def count(sessions, model) -> int:
    async with sessions() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()


# first log creates the user once and one entry per message
async def test_log_creates_user_once(sessions):
    transport = FakeTransport()
    logger = ExpenseLogger(FakeLLM(), sessions, GLOBAL)
    await log(logger, transport, "14155550100", "coffee 6.50")
    await log(logger, transport, "14155550100", "uber 14.20")
    assert await count(sessions, User) == 1
    assert await count(sessions, LedgerEntry) == 2
    to, reply, buttons = transport.sent_buttons[0]
    assert (to, reply) == ("14155550100", "✅ *Logged USD 6.50*\nDining · coffee")
    assert buttons[0][0].startswith("undo:")


# undo voids once and a second tap changes nothing
async def test_undo_voids_once(sessions):
    transport = FakeTransport()
    logger = ExpenseLogger(FakeLLM(), sessions, GLOBAL)
    await log(logger, transport, "14155550100", "coffee 6.50")
    entry_id = transport.sent_buttons[0][2][0][0].removeprefix("undo:")
    await logger.undo(text("14155550100", "1"), entry_id, transport)
    async with sessions() as session:
        first = (await session.execute(select(LedgerEntry.voided_at))).scalar_one()
    await logger.undo(text("14155550100", "1"), entry_id, transport)
    async with sessions() as session:
        second = (await session.execute(select(LedgerEntry.voided_at))).scalar_one()
    assert first is not None and first == second
    assert [reply for _, reply in transport.sent_text] == [
        "*Removed USD 6.50 · Dining · coffee*", log_module.NOTHING_TO_UNDO
    ]


# another user cannot undo this entry
async def test_undo_ignores_other_user(sessions):
    transport = FakeTransport()
    logger = ExpenseLogger(FakeLLM(), sessions, GLOBAL)
    await log(logger, transport, "14155550100", "coffee 6.50")
    entry_id = transport.sent_buttons[0][2][0][0].removeprefix("undo:")
    await logger.undo(text("6599999999", "1"), entry_id, transport)
    async with sessions() as session:
        voided = (await session.execute(select(LedgerEntry.voided_at))).scalar_one()
    assert voided is None
    assert transport.sent_text == [("6599999999", log_module.NOTHING_TO_UNDO)]


# no amount asks for one and writes nothing
async def test_missing_amount_asks(sessions):
    transport = FakeTransport()
    logger = ExpenseLogger(FakeLLM(), sessions, GLOBAL)
    stuff = text("14155550100", "bought stuff")
    await logger.log(stuff, transport, ExpenseDraft(amount_cents=None))
    assert await count(sessions, LedgerEntry) == 0
    assert "How much" in transport.sent_text[0][1]


# a receipt that adds up is saved as one receipt expense for its total
async def test_receipt_saved_once(sessions):
    receipt = ReceiptDraft(
        vendor="Cafe", category="Dining", subtotal_cents=2000, service_cents=200,
        total_cents=2200, items=[ReceiptItem(name="pasta", price_cents=2000)]
    )
    transport = FakeTransport()
    transport.media["photo-1"] = b"jpeg-bytes"
    logger = ExpenseLogger(FakeLLM({"parse_receipt": receipt}), sessions, GLOBAL)
    photo = Inbound("m1", "14155550100", "image", media_ref="photo-1")
    await logger.log_receipt(photo, transport)
    await logger.log_receipt(photo, transport)
    async with sessions() as session:
        rows = (await session.execute(select(LedgerEntry.amount_cents, LedgerEntry.source))).all()
    assert [(amount, source.value) for amount, source in rows] == [(2200, "RECEIPT")]
    assert transport.sent_buttons[0][1] == "✅ *Logged USD 22.00*\nDining · Cafe"
    assert [text for _, text in transport.sent_text][0] == READING
    assert transport.sent_text[-1][1].startswith("*Already logged*")


# a voice note echoes the transcript, goes through the router and logs a voice expense
async def test_voice_note_logged(sessions):
    transport = FakeTransport()
    transport.media["note-1"] = b"OggS" + bytes(2) + (3 * 48000).to_bytes(8, "little")
    llm = FakeLLM()
    llm.transcript = "Just spent 45 on petrol at Shell"
    logger = ExpenseLogger(llm, sessions, GLOBAL)
    router = fake_router(llm, logger=logger)
    await router.handle(Inbound("m1", "14155550100", "audio", media_ref="note-1"), transport)
    assert transport.sent_text == [("14155550100", "*I heard*\n> Just spent 45 on petrol at Shell")]
    assert transport.sent_buttons[0][1].startswith("✅ *Logged USD 45.00*\nTransport")
    async with sessions() as session:
        rows = (await session.execute(select(LedgerEntry.amount_cents, LedgerEntry.source))).all()
    assert [(amount, source.value) for amount, source in rows] == [(4500, "VOICE")]


# a confirmed mismatch is saved at the printed total
async def test_confirmed_receipt_saved(sessions):
    receipt = ReceiptDraft(total_cents=1500, subtotal_cents=900, category="Dining")
    transport = FakeTransport()
    transport.media["photo-1"] = b"jpeg-bytes"
    logger = ExpenseLogger(FakeLLM({"parse_receipt": receipt}), sessions, GLOBAL)
    await logger.log_receipt(Inbound("m1", "14155550100", "image", media_ref="photo-1"), transport)
    assert await count(sessions, LedgerEntry) == 0
    key = transport.sent_buttons[0][2][0][0].removeprefix("receipt:")
    await logger.confirm_receipt(text("14155550100", "1"), key, transport)
    assert await count(sessions, LedgerEntry) == 1


# a logged receipt can be split instead once, which removes it from spending
async def test_receipt_split_instead(sessions):
    receipt = ReceiptDraft(total_cents=1500, category="Dining")
    transport = FakeTransport()
    transport.media["photo-1"] = b"jpeg-bytes"
    logger = ExpenseLogger(FakeLLM({"parse_receipt": receipt}), sessions, GLOBAL)
    await logger.log_receipt(Inbound("m1", "14155550100", "image", media_ref="photo-1"), transport)
    undo, split = transport.sent_buttons[0][2]
    assert undo[0].startswith("undo:") and split[1] == "Split it instead"
    entry_id, _, key = split[0].removeprefix("split:").partition(":")
    tap = text("14155550100", "2")
    assert await logger.split_instead(tap, entry_id, key, transport) == receipt
    async with sessions() as session:
        voided = (await session.execute(select(LedgerEntry.voided_at))).scalar_one()
    assert voided is not None
    assert await logger.split_instead(tap, entry_id, key, transport) is None
    assert [text for _, text in transport.sent_text] == [READING, SPLIT_GONE]


# undo last shows the newest entry and removes it only once confirmed, then the one before
async def test_undo_last(sessions):
    transport = FakeTransport()
    logger = ExpenseLogger(FakeLLM(), sessions, GLOBAL)
    await log(logger, transport, "14155550100", "coffee 6.50")
    await log(logger, transport, "14155550100", "uber 14.20")
    me = text("14155550100", "/undo")

    # ask about the newest entry and tap yes
    async def confirm() -> str:
        await logger.undo_last(me, transport)
        _, ask, buttons = transport.sent_buttons[-1]
        await logger.undo(me, buttons[0][0].removeprefix("undo:"), transport)
        return ask

    assert await confirm() == "*Remove your last entry?*\nUSD 14.20 · Transport · uber"
    async with sessions() as session:
        rows = (await session.execute(select(LedgerEntry.note, LedgerEntry.voided_at))).all()
    assert {note for note, voided in rows if voided is not None} == {"uber"}
    await confirm()
    await logger.undo_last(me, transport)
    assert [reply for _, reply in transport.sent_text] == [
        "*Removed USD 14.20 · Transport · uber*", "*Removed USD 6.50 · Dining · coffee*",
        log_module.NOTHING_TO_UNDO
    ]
