from starlette.requests import Request

from splitledger.db.models import User
from splitledger.plans.quota import QuotaBook
from splitledger.split.overview import Overview
from splitledger.web.dashboard import TIP_COOKIE, DashboardPage
from tests.integration.test_plan_page import TOKENS, signed_in

TIP = "add this page to your home screen"


# the same request with the tip cookie the first visit set
def with_tip(request: Request) -> Request:
    cookie = request.headers["cookie"] + f"; {TIP_COOKIE}=1"
    return Request({"type": "http", "headers": [(b"cookie", cookie.encode())]})


# the home screen tip shows on the first visit only, and never on other tabs
async def test_home_screen_tip_once(sessions):
    user = User(wa_user_id="14155550100", currency="USD", timezone="UTC")
    async with sessions() as session:
        session.add(user)
        await session.commit()
    page = DashboardPage(TOKENS, Overview(sessions, QuotaBook(sessions)), False)
    first = await page.show(signed_in(user))
    assert TIP in first.body.decode()
    assert f"{TIP_COOKIE}=1" in first.headers["set-cookie"]
    again = await page.show(with_tip(signed_in(user)))
    assert TIP not in again.body.decode() and "set-cookie" not in again.headers
    assert TIP not in (await page.show(signed_in(user), "spending")).body.decode()
