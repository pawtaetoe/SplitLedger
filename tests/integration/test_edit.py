from datetime import UTC, datetime

from sqlalchemy import select

from splitledger.contracts.messages import Inbound
from splitledger.db.models import Bill, BillItem, User
from splitledger.handlers import bill as bill_module
from splitledger.split import edit as edit_module
from splitledger.split.claims import ClaimBook
from splitledger.split.edit import BAD_CHARGES, BAD_PRICE, NOT_YOURS, STARTED, BillEditor, Row
from splitledger.split.tokens import ClaimTokens
from tests.integration.test_group_bill import DINNER, ME, PHOTO, maker


# the bill read from a receipt whose lines don't add up, with its lines in order
async def misread(sessions) -> tuple[Bill, list[BillItem], User]:
    group, transport = maker(sessions, DINNER.model_copy(update={"total_cents": 20000}))
    await group.from_receipt(PHOTO, transport)
    async with sessions() as session:
        bill = (await session.execute(select(Bill))).scalar_one()
        items = list((await session.execute(
            select(BillItem).where(BillItem.bill_id == bill.id).order_by(BillItem.position)
        )).scalars())
        me = (await session.execute(select(User).where(User.wa_user_id == ME))).scalar_one()
    return bill, items, me


# a price that can't be read is named, and nothing is saved
async def test_bad_price_named(sessions):
    bill, _, me = await misread(sessions)
    editor = BillEditor(sessions)
    rows = [Row("", "Tea", "2"), Row("", "Cake", "abc")]
    assert await editor.save(bill.code, me.id, "", rows, "") == BAD_PRICE.format("Cake")
    assert await editor.save(bill.code, me.id, "", rows[:1], "lots") == BAD_CHARGES
    async with sessions() as session:
        names = (await session.execute(select(BillItem.name))).scalars().all()
    assert "Tea" not in names


# a discount bigger than the charges saves as a discount, never more than the items
async def test_net_discount(sessions):
    bill, _, me = await misread(sessions)
    editor = BillEditor(sessions)
    rows = [Row("", "Tea", "20")]
    assert await editor.save(bill.code, me.id, "", rows, "-25") == edit_module.TOO_MUCH_OFF
    assert await editor.save(bill.code, me.id, "", rows, "-5.50") is None
    async with sessions() as session:
        saved = await session.get(Bill, bill.id)
    assert saved is not None
    assert (saved.service, saved.discount, saved.total) == (0, 550, 1450)


# fix the items sends the payer a link that opens this bill for them only
async def test_fix_link(sessions):
    group, transport = maker(sessions, DINNER.model_copy(update={"total_cents": 20000}))
    await group.from_receipt(PHOTO, transport)
    async with sessions() as session:
        bill = (await session.execute(select(Bill))).scalar_one()
    tap = Inbound("m2", ME, "button", button_id=f"bill:edit:{bill.code}")
    await group.on_button(tap, tap.button_id or "", transport)
    to, text, label, url = transport.sent_links[-1]
    assert (to, text, label) == (ME, bill_module.FIX_LINK, "Fix the items")
    assert url.startswith(f"https://sl.test/e/{bill.code}/")
    token = url.split(f"/e/{bill.code}/")[1]
    links = bill_module.edit_links(ClaimTokens("secret"), bill.code)
    assert links.read(token, datetime.now(UTC)) == str(bill.payer_id)


# changed prices, a new line and a removed one, the total follows and a fresh message goes out
async def test_save_and_resend(sessions):
    bill, items, me = await misread(sessions)
    crab, beer, _ = items
    rows = [
        Row(str(crab.id), "Grilled salmon", "100.00"), Row(str(beer.id), "Beer", "10"),
        Row("", "Rice", "1,50"), Row("", "", "")
    ]
    assert await BillEditor(sessions).save(bill.code, me.id, "Jumbo", rows, "18.80") is None
    async with sessions() as session:
        fresh = await session.get(Bill, bill.id)
        lines = list((await session.execute(
            select(BillItem.name, BillItem.price_cents, BillItem.id)
            .where(BillItem.bill_id == bill.id).order_by(BillItem.position)
        )).all())
    assert [(name, cents) for name, cents, _ in lines] == [
        ("Grilled salmon", 10000), ("Beer", 1000), ("Rice", 150)
    ]
    assert lines[0][2] == crab.id
    assert fresh is not None and (fresh.total, fresh.service, fresh.tax) == (13030, 1880, 0)
    group, transport = maker(sessions, DINNER)
    await group.edited(ME, bill.code, transport)
    assert transport.sent_buttons[-1][1].startswith(f"*Bill updated · code {bill.code}*")
    assert "3. Rice · SGD 1.50" in transport.sent_text[-1][1]


# only the payer can edit, and only until a friend joins
async def test_edit_guards(sessions):
    bill, _, me = await misread(sessions)
    editor = BillEditor(sessions)
    friend = User(wa_user_id="6591111111", display_name="Anna")
    async with sessions() as session:
        session.add(friend)
        await session.commit()
    assert await editor.load(bill.code, friend.id) == NOT_YOURS
    assert not isinstance(await editor.load(bill.code, me.id), str)
    await ClaimBook(sessions).join_user(bill.id, friend.id)
    assert await editor.load(bill.code, me.id) == STARTED
    assert await editor.save(bill.code, me.id, "", [Row("", "Tea", "2")], "") == STARTED
