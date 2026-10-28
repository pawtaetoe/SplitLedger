import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader

from splitledger.contracts.transport import Transport
from splitledger.db.session import SessionFactory
from splitledger.handlers.bill import GroupBills
from splitledger.handlers.dashboard import BILL_DAYS, COOKIE, COOKIE_DAYS, bill_links
from splitledger.handlers.log import money
from splitledger.settings import settings
from splitledger.split.claims import BillView, ClaimBook
from splitledger.split.tokens import ClaimTokens, LoginLinks

TEMPLATES = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=True
)
TRY_AGAIN = "That didn't work. Please refresh the page and try again."
SAVED = "Saved"
ONLY_PAYER = "Only the person who paid can close this bill."
CLOSED_NOTE = "Closed. Everyone has been sent what they owe."


# where back goes, the dashboard for someone signed in there, else nowhere
def back_of(request: Request) -> str | None:
    return "/me?tab=bills" if request.cookies.get(COOKIE) else None


# html from one template
def render(name: str, status: int = 200, **values: Any) -> HTMLResponse:
    return HTMLResponse(TEMPLATES.get_template(name).render(money=money, **values), status)


# the claim page, joining and claiming, all from one signed link and a signed cookie
class ClaimPage:
    # signed links and cookies, the claims store and the bot number friends claim through
    def __init__(
        self, tokens: ClaimTokens, book: ClaimBook, bot_number: str, secure: bool = False
    ) -> None:
        self._secure = secure
        self._tokens = tokens
        self._cookies = LoginLinks(tokens, COOKIE_DAYS * 86400)
        self._book = book
        self._bot_number = bot_number
        self._group: GroupBills | None = None
        self._transport: Transport | None = None

    # the bill closer and the channel to message everyone, wired in by the app
    def attach(self, group: GroupBills, transport: Transport) -> None:
        self._group = group
        self._transport = transport

    # the payer closes from the page, unclaimed items are asked about on the page
    async def close(self, token: str, request: Request, choice: str) -> Response:
        view = await self._view(token, request)
        if view is None:
            return render("claim_error.html", 404)
        user_id = self.user_of(request, view.bill.code)
        if user_id is None or self._group is None or self._transport is None:
            return await self.show(token, request, ONLY_PAYER)
        status, names = await self._group.close_for(
            user_id, view.bill.code, choice, self._transport
        )
        if status == "leftovers":
            return await self.show(token, request, leftovers=names)
        if status == "not_payer":
            return await self.show(token, request, ONLY_PAYER)
        return await self.show(token, request, notice=CLOSED_NOTE)

    # the bill code in a link, none when it is wrong or old
    def code_of(self, token: str) -> str | None:
        try:
            return self._tokens.read(token, datetime.now(UTC))
        except RuntimeError:
            return None

    # the whatsapp user signed in on this phone, none when nobody is
    # signed in from the dashboard, or holding this bill's own link when a code is given
    def user_of(self, request: Request, code: str | None = None) -> uuid.UUID | None:
        now = datetime.now(UTC)
        value = request.cookies.get(COOKIE)
        user_id = self._cookies.read(value, now) if value else None
        bill_value = request.cookies.get(f"sl_b_{code}") if code else None
        if user_id is None and code and bill_value:
            user_id = bill_links(self._tokens, code).read(bill_value, now)
        try:
            return uuid.UUID(user_id) if user_id else None
        except ValueError:
            return None

    # who this phone is on the bill, none when not signed in or not joined yet
    async def me_of(self, request: Request, code: str) -> uuid.UUID | None:
        user_id = self.user_of(request, code)
        return await self._book.person_of(code, user_id) if user_id else None

    # a personal bill link from the chat: join them, remember it on this phone, show the bill
    async def open_bill(self, code: str, token: str) -> Response:
        code = code.upper()
        now = datetime.now(UTC)
        user_id = bill_links(self._tokens, code).read(token, now)
        bill = await self._book.open_bill(code) if user_id else None
        if bill is not None and user_id is not None:
            await self._book.join_user(bill.id, uuid.UUID(user_id))
        if user_id is None:
            ask = f"https://wa.me/{self._bot_number}?text={quote(f'CLAIM {code}')}"
            return render(
                "message.html", 401, title="This link has expired",
                body="Bill links work for 14 days. Tap below to get a fresh one in WhatsApp.",
                action=ask, action_label="Get a fresh link"
            )
        response = RedirectResponse(f"/c/{self._tokens.make(code, now)}", 303)
        response.set_cookie(
            f"sl_b_{code}", token, max_age=BILL_DAYS * 86400, httponly=True, samesite="lax",
            secure=self._secure
        )
        return response

    # the page for this viewer, the friendly error when the link is bad
    async def show(
        self, token: str, request: Request, error: str | None = None, notice: str | None = None,
        leftovers: list[str] | None = None
    ) -> HTMLResponse:
        view = await self._view(token, request)
        if view is None:
            return render("claim_error.html", 404)
        chat_url = f"https://wa.me/{self._bot_number}?text={quote(f'CLAIM {view.bill.code}')}"
        return render(
            "claim.html", view=view, token=token, error=error, notice=notice,
            back=back_of(request), signed_in=self.user_of(request, view.bill.code) is not None,
            chat_url=chat_url, leftovers=leftovers
        )

    # the signed in whatsapp user joins this bill, nobody else can
    async def join(self, token: str, request: Request) -> Response:
        view = await self._view(token, request)
        if view is None:
            return render("claim_error.html", 404)
        user_id = self.user_of(request, view.bill.code)
        if user_id is not None:
            await self._book.join_user(view.bill.id, user_id)
        return RedirectResponse(f"/c/{token}", 303)

    # one tap on an item, htmx gets the new bill back and plain forms get the page
    async def claim(self, token: str, request: Request, item: str, action: str) -> Response:
        view = await self._view(token, request)
        if view is None:
            return render("claim_error.html", 404)
        if view.me is None:
            return RedirectResponse(f"/c/{token}", 303)
        try:
            error = await self._book.claim(view.bill.id, view.me.id, uuid.UUID(item), action)
        except ValueError:
            error = TRY_AGAIN
        return await self._after(token, request, error)

    # the ticked boxes become exactly this person's items
    async def set_mine(self, token: str, request: Request, items: list[str]) -> Response:
        view = await self._view(token, request)
        if view is None:
            return render("claim_error.html", 404)
        if view.me is None:
            return RedirectResponse(f"/c/{token}", 303)
        try:
            ids = {uuid.UUID(item) for item in items}
            error = await self._book.set_mine(view.bill.id, view.me.id, ids)
        except ValueError:
            error = TRY_AGAIN
        return await self._after(token, request, error, None if error else SAVED)

    # the payer switches an even split to friends ticking their own items
    async def by_item(self, token: str, request: Request) -> Response:
        view = await self._view(token, request)
        if view is None:
            return render("claim_error.html", 404)
        if view.me is None:
            return RedirectResponse(f"/c/{token}", 303)
        error = await self._book.to_items(view.bill.id, view.me.id)
        return await self._after(token, request, error)

    # htmx gets the new bill back, plain forms get the whole page
    async def _after(
        self, token: str, request: Request, error: str | None, notice: str | None = None
    ) -> Response:
        if not request.headers.get("HX-Request"):
            return await self.show(token, request, error, notice)
        fresh = await self._view(token, request)
        return render("claim_bill.html", view=fresh, token=token, error=error, notice=notice)

    # the bill as this viewer sees it
    async def _view(self, token: str, request: Request) -> BillView | None:
        code = self.code_of(token)
        return await self._book.view(code, await self.me_of(request, code)) if code else None


claim_page = ClaimPage(
    ClaimTokens(settings.app_secret), ClaimBook(SessionFactory),
    settings.whatsapp_bot_number or re.sub(r"\D", "", settings.twilio_whatsapp_from),
    settings.public_base_url.startswith("https://")
)
claim_router = APIRouter()


# the claim page
@claim_router.get("/c/{token}", response_class=HTMLResponse)
async def page(token: str, request: Request) -> HTMLResponse:
    return await claim_page.show(token, request)


# join as the signed in whatsapp user
@claim_router.post("/c/{token}/join")
async def join(token: str, request: Request) -> Response:
    return await claim_page.join(token, request)


# claim, share or drop one item
@claim_router.post("/c/{token}/claim")
async def claim(
    token: str, request: Request, item: str = Form(""), action: str = Form("")
) -> Response:
    return await claim_page.claim(token, request, item, action)


# save the ticked items
@claim_router.post("/c/{token}/mine")
async def mine(
    token: str, request: Request, items: Annotated[list[str] | None, Form()] = None
) -> Response:
    return await claim_page.set_mine(token, request, items or [])


# switch an even split to ticking items
@claim_router.post("/c/{token}/by-item")
async def by_item(token: str, request: Request) -> Response:
    return await claim_page.by_item(token, request)


# the payer closes the bill
@claim_router.post("/c/{token}/close")
async def close(token: str, request: Request, choice: Annotated[str, Form()] = "ask") -> Response:
    return await claim_page.close(token, request, choice)
