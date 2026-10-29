import re
import uuid
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from splitledger.contracts.transport import Transport
from splitledger.db.session import SessionFactory
from splitledger.handlers.dashboard import COOKIE, COOKIE_DAYS, LINK_MINUTES, PAGES
from splitledger.handlers.router import book, rates
from splitledger.handlers.settle import SettleUp
from splitledger.pay.rails import PayRails
from splitledger.settings import settings
from splitledger.split.overview import Overview
from splitledger.split.tokens import ClaimTokens, LoginLinks
from splitledger.web.claim import render

# pages a link button may open, the first part of the path
LINK_PAGES = frozenset({"b", "c", "d", "e"})
# the dashboard tabs, key and label
TABS = {"overview": "Overview", "bills": "Bills", "spending": "Spending", "coming": "Coming up"}
EXPIRED = (
    "This link has expired",
    "Links from the chat work for 15 minutes. Tap below to get a fresh one in WhatsApp."
)
SIGN_IN = (
    "Open your dashboard from WhatsApp",
    "Tap below to message the bot, then send. It replies with a link that signs this phone "
    "in for 30 days."
)


# the dashboard behind a short link from the chat and a cookie after that
class DashboardPage:
    # the signer for links and cookies, the overview reader and whether cookies need https
    def __init__(
        self, tokens: ClaimTokens, overview: Overview, secure: bool, bot_number: str = ""
    ) -> None:
        self._bot_number = bot_number
        self._tokens = tokens
        self._links = LoginLinks(tokens, LINK_MINUTES * 60)
        self._cookies = LoginLinks(tokens, COOKIE_DAYS * 86400)
        self._overview = overview
        self._secure = secure
        self._rails = PayRails()
        self._settle: SettleUp | None = None
        self._transport: Transport | None = None

    # settling up and the transport that tells both people, set once the app is wired
    def attach(self, settle: SettleUp, rails: PayRails, transport: Transport) -> None:
        self._settle = settle
        self._rails = rails
        self._transport = transport

    # swap the short link for a cookie and go to the dashboard
    # an old link still works on a phone that is already signed in
    def sign_in(self, token: str, request: Request, to: str = "me") -> Response:
        now = datetime.now(UTC)
        user_id = self._links.read(token, now)
        if user_id is None and self.user_of(request) is not None:
            return RedirectResponse(self._target(to, now), 303)
        if user_id is None:
            code = to.removeprefix("bill-") if to.startswith("bill-") else ""
            ask = f"CLAIM {code}" if code.isalnum() and code else "dashboard"
            action = f"https://wa.me/{self._bot_number}?text={quote(ask)}"
            return render(
                "message.html", 401, title=EXPIRED[0], body=EXPIRED[1], action=action,
                action_label="Get a fresh link"
            )
        response = RedirectResponse(self._target(to, now), 303)
        response.set_cookie(
            COOKIE, self._cookies.make(user_id, now), max_age=COOKIE_DAYS * 86400,
            httponly=True, samesite="lax", secure=self._secure
        )
        return response

    # where a sign in link goes: a page we know or one bill by its code, else the dashboard
    def _target(self, to: str, now: datetime) -> str:
        code = to.removeprefix("bill-")
        if to.startswith("bill-") and code.isalnum() and len(code) <= 8:
            return f"/c/{self._tokens.make(code.upper(), now)}"
        return f"/{to if to in PAGES else 'me'}"

    # the signed in user from the cookie, none when missing, edited or old
    def user_of(self, request: Request) -> uuid.UUID | None:
        value = request.cookies.get(COOKIE)
        user_id = self._cookies.read(value, datetime.now(UTC)) if value else None
        try:
            return uuid.UUID(user_id) if user_id else None
        except ValueError:
            return None

    # the dashboard for the signed in user on one tab, or how to sign in
    async def show(self, request: Request, tab: str = "overview", paid: str = "") -> HTMLResponse:
        user_id = self.user_of(request)
        board = await self._overview.build(user_id) if user_id else None
        if board is None:
            action = f"https://wa.me/{self._bot_number}?text=dashboard"
            return render(
                "message.html", 401, title=SIGN_IN[0], body=SIGN_IN[1], action=action,
                action_label="Get a link"
            )
        links = {
            open_bill.bill.code: f"/c/{self._tokens.make(open_bill.bill.code, datetime.now(UTC))}"
            for open_bill in [*board.paid, *board.joined]
        }
        pay = {
            str(b.user): self._rails.offer(b.wa, -b.cents, b.currency, "SplitLedger").label
            for b in board.people() if b.cents < 0 and b.user and b.wa
        }
        return render(
            "dashboard.html", board=board, links=links, tz=ZoneInfo(board.user.timezone),
            tab=tab if tab in TABS else "overview", tabs=TABS, pay=pay, paid=paid
        )

    # i've paid from the page, the same as the chat button, both people get a message
    async def paid(self, request: Request, to: str, currency: str) -> Response:
        user_id = self.user_of(request)
        if user_id is None:
            return render("message.html", 401, title=SIGN_IN[0], body=SIGN_IN[1])
        try:
            creditor = uuid.UUID(to)
        except ValueError:
            return RedirectResponse("/me", 303)
        if self._settle is None or self._transport is None:
            return RedirectResponse("/me", 303)
        cents = await self._settle.pay(user_id, creditor, currency, self._transport)
        return RedirectResponse(f"/me?paid={quote(currency)}" if cents else "/me", 303)


overview = Overview(SessionFactory, book, rates)
dashboard_page = DashboardPage(
    ClaimTokens(settings.app_secret), overview,
    settings.public_base_url.startswith("https://"),
    settings.whatsapp_bot_number or re.sub(r"\D", "", settings.twilio_whatsapp_from)
)
dashboard_router = APIRouter()


# a link button from the chat, its path has ~ for / so it fits one template variable
@dashboard_router.get("/l/{key}")
async def link(key: str, request: Request) -> Response:
    path = key.replace("~", "/")
    if path.split("/", 1)[0] not in LINK_PAGES or "//" in path or "\\" in path:
        return RedirectResponse("/me", 303)
    query = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(f"/{path}{query}", 303)


# the link from the chat
@dashboard_router.get("/d/{token}")
async def sign_in(token: str, request: Request, to: str = "me") -> Response:
    return dashboard_page.sign_in(token, request, to)


# the dashboard
@dashboard_router.get("/me", response_class=HTMLResponse)
async def me(request: Request, tab: str = "overview", paid: str = "") -> HTMLResponse:
    return await dashboard_page.show(request, tab, paid)


# i've paid from the dashboard
@dashboard_router.post("/me/paid")
async def paid(
    request: Request, to: Annotated[str, Form()], currency: Annotated[str, Form()]
) -> Response:
    return await dashboard_page.paid(request, to, currency)
