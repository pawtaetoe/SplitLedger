import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from splitledger.db.session import SessionFactory
from splitledger.handlers.dashboard import LINK_MINUTES, PAGES
from splitledger.plans.quota import QuotaBook
from splitledger.settings import settings
from splitledger.split.overview import Overview
from splitledger.split.tokens import ClaimTokens, LoginLinks
from splitledger.web.claim import render

COOKIE = "sl_me"
# the dashboard tabs, key and label
TABS = {"overview": "Overview", "bills": "Bills", "spending": "Spending", "coming": "Coming up"}
COOKIE_DAYS = 30
EXPIRED = (
    "This link has expired",
    "Dashboard links work for 15 minutes. Send menu in WhatsApp and tap My dashboard again."
)
SIGN_IN = (
    "Open your dashboard from WhatsApp",
    "Send menu to the bot and tap My dashboard. The link signs this phone in for 30 days."
)


# the dashboard behind a short link from the chat and a cookie after that
class DashboardPage:
    # the signer for links and cookies, the overview reader and whether cookies need https
    def __init__(self, tokens: ClaimTokens, overview: Overview, secure: bool) -> None:
        self._tokens = tokens
        self._links = LoginLinks(tokens, LINK_MINUTES * 60)
        self._cookies = LoginLinks(tokens, COOKIE_DAYS * 86400)
        self._overview = overview
        self._secure = secure

    # swap the short link for a cookie and go to the dashboard
    def sign_in(self, token: str, to: str = "me") -> Response:
        now = datetime.now(UTC)
        user_id = self._links.read(token, now)
        if user_id is None:
            return render("message.html", 401, title=EXPIRED[0], body=EXPIRED[1])
        response = RedirectResponse(f"/{to if to in PAGES else 'me'}", 303)
        response.set_cookie(
            COOKIE, self._cookies.make(user_id, now), max_age=COOKIE_DAYS * 86400,
            httponly=True, samesite="lax", secure=self._secure
        )
        return response

    # the signed in user from the cookie, none when missing, edited or old
    def user_of(self, request: Request) -> uuid.UUID | None:
        value = request.cookies.get(COOKIE)
        user_id = self._cookies.read(value, datetime.now(UTC)) if value else None
        try:
            return uuid.UUID(user_id) if user_id else None
        except ValueError:
            return None

    # the dashboard for the signed in user on one tab, or how to sign in
    async def show(self, request: Request, tab: str = "overview") -> HTMLResponse:
        user_id = self.user_of(request)
        board = await self._overview.build(user_id) if user_id else None
        if board is None:
            return render("message.html", 401, title=SIGN_IN[0], body=SIGN_IN[1])
        links = {
            open_bill.bill.code: f"/c/{self._tokens.make(open_bill.bill.code, datetime.now(UTC))}"
            for open_bill in [*board.paid, *board.joined]
        }
        return render(
            "dashboard.html", board=board, links=links, tz=ZoneInfo(board.user.timezone),
            tab=tab if tab in TABS else "overview", tabs=TABS
        )


dashboard_page = DashboardPage(
    ClaimTokens(settings.app_secret), Overview(SessionFactory, QuotaBook(SessionFactory)),
    settings.public_base_url.startswith("https://")
)
dashboard_router = APIRouter()


# the link from the chat
@dashboard_router.get("/d/{token}")
async def sign_in(token: str, to: str = "me") -> Response:
    return dashboard_page.sign_in(token, to)


# the dashboard
@dashboard_router.get("/me", response_class=HTMLResponse)
async def me(request: Request, tab: str = "overview") -> HTMLResponse:
    return await dashboard_page.show(request, tab)
