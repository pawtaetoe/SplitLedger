import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader

from splitledger.db.session import SessionFactory
from splitledger.handlers.log import money
from splitledger.settings import settings
from splitledger.split.claims import BillView, ClaimBook
from splitledger.split.tokens import TOKEN_DAYS, ClaimTokens

TEMPLATES = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=True
)
TRY_AGAIN = "That didn't work. Please refresh the page and try again."
NEED_NAME = "Please type your name."


# html from one template
def render(name: str, status: int = 200, **values: Any) -> HTMLResponse:
    return HTMLResponse(TEMPLATES.get_template(name).render(money=money, **values), status)


# the claim page, joining and claiming, all from one signed link and a signed cookie
class ClaimPage:
    # signed links and cookies, and the claims store
    def __init__(self, tokens: ClaimTokens, book: ClaimBook, secure: bool) -> None:
        self._tokens = tokens
        self._book = book
        self._secure = secure

    # the bill code in a link, none when it is wrong or old
    def code_of(self, token: str) -> str | None:
        try:
            return self._tokens.read(token, datetime.now(UTC))
        except RuntimeError:
            return None

    # who this browser is on this bill, from its signed cookie
    def me_of(self, request: Request, code: str) -> uuid.UUID | None:
        sealed = request.cookies.get(f"sl_{code}")
        value = self._tokens.unseal(sealed) if sealed else None
        try:
            return uuid.UUID(value) if value else None
        except ValueError:
            return None

    # the page for this viewer, the friendly error when the link is bad
    async def show(self, token: str, request: Request, error: str | None = None) -> HTMLResponse:
        view = await self._view(token, request)
        if view is None:
            return render("claim_error.html", 404)
        return render("claim.html", view=view, token=token, error=error)

    # remember who this browser is, by a name typed or tapped
    async def join(self, token: str, request: Request, name: str) -> Response:
        view = await self._view(token, request)
        if view is None:
            return render("claim_error.html", 404)
        try:
            person = await self._book.join(view.bill.id, name)
        except ValueError:
            return await self.show(token, request, NEED_NAME)
        response = RedirectResponse(f"/c/{token}", 303)
        response.set_cookie(
            f"sl_{view.bill.code}", self._tokens.seal(str(person)), max_age=TOKEN_DAYS * 86400,
            httponly=True, samesite="lax", secure=self._secure
        )
        return response

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
        return await self._after(token, request, error)

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
    async def _after(self, token: str, request: Request, error: str | None) -> Response:
        if not request.headers.get("HX-Request"):
            return await self.show(token, request, error)
        fresh = await self._view(token, request)
        return render("claim_bill.html", view=fresh, token=token, error=error)

    # the bill as this viewer sees it
    async def _view(self, token: str, request: Request) -> BillView | None:
        code = self.code_of(token)
        return await self._book.view(code, self.me_of(request, code)) if code else None


claim_page = ClaimPage(
    ClaimTokens(settings.app_secret), ClaimBook(SessionFactory),
    settings.public_base_url.startswith("https://")
)
claim_router = APIRouter()


# the claim page
@claim_router.get("/c/{token}", response_class=HTMLResponse)
async def page(token: str, request: Request) -> HTMLResponse:
    return await claim_page.show(token, request)


# pick or type a name
@claim_router.post("/c/{token}/join")
async def join(token: str, request: Request, name: str = Form("")) -> Response:
    return await claim_page.join(token, request, name)


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
