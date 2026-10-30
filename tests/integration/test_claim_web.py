from datetime import UTC, datetime

from sqlalchemy import select
from starlette.requests import Request

from splitledger.db.models import Bill, BillStatus, Participant, User
from splitledger.handlers.bill import GroupBills
from splitledger.handlers.dashboard import COOKIE, bill_links
from splitledger.handlers.log import ExpenseLogger
from splitledger.llm.schemas import GLOBAL
from splitledger.split.claims import ClaimBook
from splitledger.split.tokens import ClaimTokens, LoginLinks
from splitledger.web.claim import ClaimPage
from tests.fakes import FakeLLM, FakeTransport
from tests.integration.test_claims import seed

TOKENS = ClaimTokens("secret")


# a request from a phone signed in as this user, or from nobody
def phone(user: User | None = None) -> Request:
    headers = []
    if user is not None:
        cookie = LoginLinks(TOKENS, 3600).make(str(user.id), datetime.now(UTC))
        headers.append((b"cookie", f"{COOKIE}={cookie}".encode()))
    return Request({"type": "http", "headers": headers})


# people on the bill with their whatsapp user
async def people(sessions) -> list[tuple[str, object]]:
    async with sessions() as session:
        rows = (await session.execute(select(Participant.guest_name, Participant.user_id))).all()
    return [(name, user) for name, user in rows]


# a stranger with the link can only look and is sent to whatsapp
# a signed in user joins as themselves
async def test_only_whatsapp_users_join(sessions):
    bill, _, _ = await seed(sessions)
    page = ClaimPage(TOKENS, ClaimBook(sessions), "6512345678")
    token = TOKENS.make(bill.code, datetime.now(UTC))
    stranger = await page.show(token, phone())
    assert "Open WhatsApp to claim" in stranger.body.decode()
    assert "wa.me/6512345678?text=CLAIM%20K7Q2AB" in stranger.body.decode()
    await page.join(token, phone())
    assert len(await people(sessions)) == 1
    anna = User(wa_user_id="6591111111", currency="SGD", timezone="Asia/Singapore",
                display_name="Anna")
    async with sessions() as session:
        session.add(anna)
        await session.commit()
    before = await page.show(token, phone(anna))
    assert "Join this bill" in before.body.decode()
    await page.join(token, phone(anna))
    assert ("Anna", anna.id) in await people(sessions)
    after = await page.show(token, phone(anna))
    assert "Tick everything you had" in after.body.decode()



# the personal link from the chat joins that person on that bill only, and old ones say so
async def test_personal_bill_link(sessions):
    bill, _, _ = await seed(sessions)
    page = ClaimPage(TOKENS, ClaimBook(sessions), "6512345678")
    ben = User(wa_user_id="6592222222", currency="SGD", timezone="Asia/Singapore",
               display_name="Ben")
    async with sessions() as session:
        session.add(ben)
        await session.commit()
    link = bill_links(TOKENS, bill.code).make(str(ben.id), datetime.now(UTC))
    opened = await page.open_bill(bill.code.lower(), link)
    assert opened.status_code == 303 and opened.headers["location"].startswith("/c/")
    assert f"sl_b_{bill.code}=" in opened.headers["set-cookie"]
    token = opened.headers["location"].removeprefix("/c/")
    cookie = (b"cookie", f"sl_b_{bill.code}={link}".encode())
    # opening the personal link joins them, so the page goes straight to ticking
    assert ("Ben", ben.id) in await people(sessions)
    shown = await page.show(token, Request({"type": "http", "headers": [cookie]}))
    assert "Tick everything you had" in shown.body.decode()
    old = bill_links(TOKENS, bill.code).make(str(ben.id), datetime(2026, 1, 1, tzinfo=UTC))
    assert (await page.open_bill(bill.code, old)).status_code == 401


# the payer can close from the page, unclaimed items are asked about there, others cannot
async def test_payer_closes_on_the_page(sessions):
    bill, _, _ = await seed(sessions)
    async with sessions() as session:
        payer = await session.get(User, bill.payer_id)
    assert payer is not None
    page = ClaimPage(TOKENS, ClaimBook(sessions), "6512345678")
    logger = ExpenseLogger(FakeLLM(), sessions, GLOBAL)
    transport = FakeTransport()
    page.attach(GroupBills(sessions, logger, GLOBAL, TOKENS, "https://sl.test", "1"), transport)
    token = TOKENS.make(bill.code, datetime.now(UTC))
    assert "Close the bill now" in (await page.show(token, phone(payer))).body.decode()
    refused = await page.close(token, phone(), "ask")
    assert "Only the person who paid" in refused.body.decode()
    asked = await page.close(token, phone(payer), "ask")
    assert "Nobody claimed Pasta, Pasta, Calamari" in asked.body.decode()
    done = await page.close(token, phone(payer), "mine")
    assert "Closed. Everyone has been sent what they owe." in done.body.decode()
    async with sessions() as session:
        stored = await session.get(Bill, bill.id)
    assert stored is not None and stored.status == BillStatus.FINALISED
    assert transport.sent_links[-1][0] == payer.wa_user_id
