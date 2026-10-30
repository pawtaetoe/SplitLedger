from sqlalchemy import select

from splitledger.contracts.messages import Inbound
from splitledger.db.models import Bill, BillItem, Participant, User
from splitledger.handlers import bill as bill_module
from splitledger.handlers.bill import GroupBills
from splitledger.handlers.log import ExpenseLogger
from splitledger.llm.schemas import GLOBAL, ReceiptDraft, ReceiptItem
from splitledger.split.tokens import ClaimTokens
from tests.fakes import FakeLLM, FakeTransport

ME = "6581234567"
DINNER = ReceiptDraft(
    vendor="Jumbo", currency="SGD", category="Dining",
    items=[
        ReceiptItem(name="Chilli crab", price_cents=8000),
        ReceiptItem(name="Beer", qty=2, price_cents=2000)
    ],
    subtotal_cents=10000, service_cents=1000, tax_cents=880, total_cents=11880
)


# a group bill maker reading the given receipt from any photo
def maker(sessions, receipt: ReceiptDraft) -> tuple[GroupBills, FakeTransport]:
    logger = ExpenseLogger(FakeLLM({"parse_receipt": receipt}), sessions, GLOBAL)
    tokens = ClaimTokens("secret")
    group = GroupBills(sessions, logger, GLOBAL, tokens, "https://sl.test/", "6512345678")
    transport = FakeTransport()
    transport.media["photo-1"] = b"jpeg"
    return group, transport


# a photo with the split caption
PHOTO = Inbound("m1", ME, "image", text="split", media_ref="photo-1")


# the bill, its items and the payer are saved and the links point at that bill
async def test_bill_from_receipt(sessions):
    group, transport = maker(sessions, DINNER)
    await group.from_receipt(PHOTO, transport)
    async with sessions() as session:
        bill = (await session.execute(select(Bill))).scalar_one()
        items = (await session.execute(
            select(BillItem).where(BillItem.bill_id == bill.id).order_by(BillItem.position)
        )).scalars().all()
        payer = (await session.execute(select(Participant))).scalar_one()
        me = (await session.execute(select(User).where(User.wa_user_id == ME))).scalar_one()
    assert (bill.total, bill.currency, bill.payer_id) == (11880, "SGD", me.id)
    assert sum(item.price_cents for item in items) == bill.subtotal == 10000
    # two beers become one row each so different people can have them
    assert [item.name for item in items] == ["Chilli crab", "Beer", "Beer"]
    assert payer.user_id == me.id
    _, ready, buttons = transport.sent_buttons[-1]
    forward = transport.sent_text[-1][1]
    assert ready == bill_module.READY.format(bill.code)
    # no close button before anyone has joined
    assert buttons == [bill_module.edit_button(bill)]
    assert f"https://wa.me/6512345678?text=CLAIM%20{bill.code}" in forward
    assert "https://sl.test/c/" not in forward


# a receipt with no items makes no bill
async def test_unsplittable_receipts(sessions):
    group, transport = maker(sessions, ReceiptDraft(total_cents=5000))
    await group.from_receipt(PHOTO, transport)
    assert transport.sent_text[-1][1] == bill_module.NO_ITEMS
    async with sessions() as session:
        assert (await session.execute(select(Bill))).first() is None


# lines that don't add up make a bill the payer fixes before anything goes to friends
async def test_not_adding_up_asks_to_fix(sessions):
    group, transport = maker(sessions, DINNER.model_copy(update={"total_cents": 20000}))
    assert await group.from_receipt(PHOTO, transport) is None
    async with sessions() as session:
        bill = (await session.execute(select(Bill))).scalar_one()
    to, text, buttons = transport.sent_buttons[-1]
    assert text == bill_module.FIX_FIRST.format("SGD 118.80", "SGD 200.00")
    assert buttons == [bill_module.edit_button(bill)]
    assert transport.sent_text == []


# a clash with an existing code is retried with a fresh one
async def test_code_clash_retries(sessions, monkeypatch):
    group, transport = maker(sessions, DINNER)
    codes = iter(["AAAAAA", "AAAAAA", "BBBBBB"])
    monkeypatch.setattr(bill_module, "new_code", lambda: next(codes))
    await group.from_receipt(PHOTO, transport)
    await group.from_receipt(PHOTO, transport)
    async with sessions() as session:
        found = (await session.execute(select(Bill.code).order_by(Bill.code))).scalars().all()
    assert found == ["AAAAAA", "BBBBBB"]


# an even split needs only the total and the payer covers the odd cents
async def test_even_split_from_photo(sessions):
    group, transport = maker(sessions, ReceiptDraft(currency="CHF", total_cents=25669))
    await group.from_receipt(PHOTO, transport, 4)
    async with sessions() as session:
        bill = (await session.execute(select(Bill))).scalar_one()
    assert bill.even_ways == 4
    ready, forward = transport.sent_text[-2][1], transport.sent_text[-1][1]
    assert ready == bill_module.READY_EVEN.format(4, bill.code, "CHF 64.17", "CHF 64.18")
    assert "Split evenly 4 ways: *CHF 64.17* each." in forward


# a message after the photo switches the latest bill to even, with nothing to switch it says so
async def test_split_evenly_after(sessions):
    group, transport = maker(sessions, DINNER)
    text = Inbound("m2", ME, "text", text="split evenly among 4 pax")
    await group.split_evenly(text, transport, 4)
    assert transport.sent_text[-1][1] == bill_module.NO_BILL.format(4)
    await group.from_receipt(PHOTO, transport)
    await group.split_evenly(text, transport, 4)
    async with sessions() as session:
        bill = (await session.execute(select(Bill))).scalar_one()
    assert bill.even_ways == 4
    assert "Split evenly 4 ways: *SGD 29.70* each." in transport.sent_text[-1][1]
    await group.split_evenly(text, transport, 1)
    assert transport.sent_text[-1][1] == bill_module.BAD_WAYS


# an unsure split saves the bill quietly, asks how, and sends the usual messages once answered
async def test_unsure_asks_how(sessions):
    group, transport = maker(sessions, DINNER)
    assert await group.from_receipt(PHOTO, transport, 4, unsure=True) is None
    _, ask, buttons = transport.sent_buttons[-1]
    assert ask == bill_module.HOW.format("SGD 118.80", bill_module.HOW_ITEMS)
    assert [title for _, title in buttons] == ["By what each had", "Evenly, 4 ways"]
    assert transport.sent_text == []
    even = buttons[1][0]
    await group.on_button(Inbound("m2", ME, "button", button_id=even), even, transport)
    async with sessions() as session:
        bill = (await session.execute(select(Bill))).scalar_one()
    assert bill.even_ways == 4
    assert "Split evenly 4 ways: *SGD 29.70* each." in transport.sent_text[-1][1]


# a receipt that doesn't add up offers even only, and even without a head count asks how many
async def test_unsure_even_only(sessions):
    group, transport = maker(sessions, DINNER.model_copy(update={"total_cents": 20000}))
    await group.from_receipt(PHOTO, transport, None, unsure=True)
    _, ask, buttons = transport.sent_buttons[-1]
    assert ask.endswith(bill_module.HOW_EVEN_ONLY)
    assert [title for _, title in buttons] == ["Fix the items", "Evenly"]
    even = buttons[1][0]
    await group.on_button(Inbound("m2", ME, "button", button_id=even), even, transport)
    _, ask, heads = transport.sent_buttons[-1]
    assert ask == bill_module.HOW_MANY
    assert [title for _, title in heads] == [f"{n} people" for n in range(2, 9)]
    three = heads[1][0]
    await group.on_button(Inbound("m3", ME, "button", button_id=three), three, transport)
    assert "Split evenly 3 ways" in transport.sent_text[-1][1]
