from datetime import UTC, datetime

from sqlalchemy import select
from starlette.requests import Request

from splitledger.db.models import BudgetPlan, User
from splitledger.handlers.plan import PlanManager
from splitledger.llm.schemas import GLOBAL
from splitledger.plans.quota import QuotaBook
from splitledger.split.overview import Overview
from splitledger.split.tokens import ClaimTokens, LoginLinks
from splitledger.web.dashboard import COOKIE, DashboardPage
from splitledger.web.plan import PlanForm, PlanPage
from tests.fakes import FakeLLM, FakeTransport
from tests.integration.test_plans import msg

TOKENS = ClaimTokens("secret")
ME = "6581234567"


# a request carrying a signed in cookie for this user
def signed_in(user: User) -> Request:
    cookie = LoginLinks(TOKENS, 3600).make(str(user.id), datetime.now(UTC))
    return Request({"type": "http", "headers": [(b"cookie", f"{COOKIE}={cookie}".encode())]})


# a plan page on the test database
def page(sessions) -> PlanPage:
    dashboard = DashboardPage(TOKENS, Overview(sessions, QuotaBook(sessions)), False)
    return PlanPage(sessions, dashboard, TOKENS, FakeLLM())


# the plan and pay the user has saved
async def stored(sessions) -> tuple[BudgetPlan, int | None]:
    async with sessions() as session:
        plan = (await session.execute(select(BudgetPlan))).scalar_one()
        pay = (await session.execute(select(User.take_home_cents))).scalar_one()
    return plan, pay


# saving on the page writes the same plan the chat writes, a bad pay saves nothing
async def test_page_saves_like_the_chat(sessions):
    user = User(wa_user_id=ME, currency="USD", timezone="UTC")
    async with sessions() as session:
        session.add(user)
        await session.commit()
    plans = page(sessions)
    refused = await plans.save(signed_in(user), PlanForm("pyf", "15", pay="lots"))
    assert refused.status_code == 400
    async with sessions() as session:
        assert (await session.execute(select(BudgetPlan))).first() is None
    saved = await plans.save(signed_in(user), PlanForm("pyf", "15", pay="4,500", period="weekly"))
    assert saved.status_code == 303 and saved.headers["location"] == "/me"
    web, pay = await stored(sessions)
    chat = PlanManager(sessions, QuotaBook(sessions), GLOBAL, FakeLLM())
    transport = FakeTransport()
    for button in ["plan:setup", "plan:fw:pyf", "plan:pct:15"]:
        await chat.on_button(msg("button", button), button, transport)
    await chat.on_text(msg("text", "4500"), transport)
    for button in ["plan:weekly", "plan:save"]:
        await chat.on_button(msg("button", button), button, transport)
    again, pay_again = await stored(sessions)
    assert (web.framework, web.period, web.buckets, web.category_map, web.step_up_pct) == (
        again.framework, again.period, again.buckets, again.category_map, again.step_up_pct
    )
    assert pay == pay_again == 450000


# without a cookie the form explains how to sign in
async def test_page_needs_sign_in(sessions):
    response = await page(sessions).show(Request({"type": "http", "headers": []}))
    assert response.status_code == 401
