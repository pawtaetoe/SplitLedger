import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse

from splitledger.contracts.transport import Transport
from splitledger.db.models import User
from splitledger.db.session import SessionFactory
from splitledger.handlers.bill import GroupBills, edit_links
from splitledger.settings import settings
from splitledger.split.edit import BillEditor, Row
from splitledger.split.tokens import ClaimTokens
from splitledger.web.claim import render

EXPIRED = (
    "This link has expired",
    "Tap Fix the items in WhatsApp again for a fresh one."
)
DONE = ("Saved", "Check WhatsApp. There's a fresh message to forward to your group.")


# the payer fixes the lines of a misread receipt before friends claim
class EditPage:
    # the link signer and the bill editor
    def __init__(self, tokens: ClaimTokens, editor: BillEditor) -> None:
        self._tokens = tokens
        self._editor = editor
        self._group: GroupBills | None = None
        self._transport: Transport | None = None

    # the group bills that send the fresh message, set once the app is wired
    def attach(self, group: GroupBills, transport: Transport) -> None:
        self._group = group
        self._transport = transport

    # the payer from the link, none when it is bad or old
    def payer_of(self, code: str, token: str) -> uuid.UUID | None:
        user_id = edit_links(self._tokens, code).read(token, datetime.now(UTC))
        try:
            return uuid.UUID(user_id) if user_id else None
        except ValueError:
            return None

    # the form with every line
    async def show(self, code: str, token: str, error: str | None = None) -> HTMLResponse:
        payer = self.payer_of(code, token)
        if payer is None:
            return render("message.html", 401, title=EXPIRED[0], body=EXPIRED[1])
        found = await self._editor.load(code, payer)
        if isinstance(found, str):
            return render("message.html", 409, title="Can't change this bill", body=found)
        bill, items = found
        return render("edit.html", bill=bill, items=items, token=token, error=error)

    # save the lines, then send the payer a fresh message to forward
    async def save(
        self, code: str, token: str, vendor: str, rows: list[Row], charges: str
    ) -> HTMLResponse:
        payer = self.payer_of(code, token)
        if payer is None:
            return render("message.html", 401, title=EXPIRED[0], body=EXPIRED[1])
        error = await self._editor.save(code, payer, vendor, rows, charges)
        if error is not None:
            return await self.show(code, token, error)
        if self._group is not None and self._transport is not None:
            async with SessionFactory() as session:
                user = await session.get(User, payer)
            if user is not None:
                await self._group.edited(user.wa_user_id, code.upper(), self._transport)
        return render("message.html", title=DONE[0], body=DONE[1])


edit_page = EditPage(ClaimTokens(settings.app_secret), BillEditor(SessionFactory))
edit_router = APIRouter()


# the form
@edit_router.get("/e/{code}/{token}", response_class=HTMLResponse)
async def form(code: str, token: str) -> HTMLResponse:
    return await edit_page.show(code, token)


# save it
@edit_router.post("/e/{code}/{token}", response_class=HTMLResponse)
async def save(
    code: str, token: str, vendor: Annotated[str, Form()] = "",
    item: Annotated[list[str] | None, Form()] = None,
    name: Annotated[list[str] | None, Form()] = None,
    price: Annotated[list[str] | None, Form()] = None,
    charges: Annotated[str, Form()] = ""
) -> HTMLResponse:
    names, prices = name or [], price or []
    ids = (item or []) + [""] * (len(names) - len(item or []))
    rows = [Row(i, n, p) for i, n, p in zip(ids, names, prices, strict=False)]
    return await edit_page.save(code, token, vendor, rows, charges)
