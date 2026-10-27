import hashlib
import hmac
import json
from typing import Any

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.exc import IntegrityError

from splitledger.contracts.messages import Inbound
from splitledger.contracts.transport import Transport
from splitledger.db.models import ProcessedMessage
from splitledger.db.session import SessionFactory
from splitledger.handlers.router import Router, message_router
from splitledger.jobs.queue import JobQueue
from splitledger.settings import settings

GRAPH_BASE = "https://graph.facebook.com/v21.0"


# check a meta signature header against the raw body
def verify_signature(body: bytes, header: str | None, secret: str) -> bool:
    if not header or not secret:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


# turn one webhook payload into inbound messages
def parse_inbound(payload: dict[str, Any]) -> list[Inbound]:
    inbound: list[Inbound] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for message in change.get("value", {}).get("messages", []):
                parsed = _parse_message(message)
                if parsed is not None:
                    inbound.append(parsed)
    return inbound


# parse one whatsapp message object
def _parse_message(message: dict[str, Any]) -> Inbound | None:
    user_id = str(message.get("from", ""))
    message_id = str(message.get("id", ""))
    kind = message.get("type")
    if kind == "text":
        return Inbound(message_id, user_id, "text", text=message.get("text", {}).get("body"))
    if kind == "image":
        image = message.get("image", {})
        return Inbound(
            message_id, user_id, "image", text=image.get("caption"), media_ref=image.get("id")
        )
    if kind == "audio":
        return Inbound(message_id, user_id, "audio", media_ref=message.get("audio", {}).get("id"))
    if kind == "interactive":
        reply = message.get("interactive", {}).get("button_reply", {})
        return Inbound(
            message_id, user_id, "button", text=reply.get("title"), button_id=reply.get("id")
        )
    return None


# whatsapp cloud api transport
class WhatsAppTransport:
    # credentials default from settings
    def __init__(
        self, access_token: str | None = None, phone_number_id: str | None = None
    ) -> None:
        self._token = access_token or settings.whatsapp_access_token
        self._phone = phone_number_id or settings.whatsapp_phone_number_id

    # auth header for graph calls
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    # post one message payload to the graph api
    async def _post_message(self, payload: dict[str, Any]) -> None:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{GRAPH_BASE}/{self._phone}/messages", json=payload, headers=self._headers()
            )
            if response.is_error:
                error = response.json().get("error", {})
                raise RuntimeError(f"graph {error.get('code')} {error.get('message')}")

    # send one text message
    async def send_text(self, to: str, text: str) -> None:
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text}
        }
        await self._post_message(payload)

    # send text with reply buttons
    async def send_buttons(self, to: str, text: str, buttons: list[tuple[str, str]]) -> None:
        shown = [
            {"type": "reply", "reply": {"id": button_id, "title": title}}
            for button_id, title in buttons
        ]
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": text},
                "action": {"buttons": shown}
            }
        }
        await self._post_message(payload)

    # upload then send one image
    async def send_image(self, to: str, png: bytes, caption: str | None = None) -> None:
        async with httpx.AsyncClient(timeout=10) as client:
            upload = await client.post(
                f"{GRAPH_BASE}/{self._phone}/media",
                data={"messaging_product": "whatsapp"},
                files={"file": ("image.png", png, "image/png")},
                headers=self._headers()
            )
            upload.raise_for_status()
            media_id = str(upload.json()["id"])
        image: dict[str, Any] = {"id": media_id}
        if caption is not None:
            image["caption"] = caption
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "image",
            "image": image
        }
        await self._post_message(payload)

    # fetch media bytes by media id
    async def fetch_media(self, media_ref: str) -> bytes:
        async with httpx.AsyncClient(timeout=10) as client:
            lookup = await client.get(f"{GRAPH_BASE}/{media_ref}", headers=self._headers())
            lookup.raise_for_status()
            download = await client.get(str(lookup.json()["url"]), headers=self._headers())
            download.raise_for_status()
            return download.content


# webhook side, dedupe then queue then route
class WhatsAppChannel:
    # wire one transport to a message router
    def __init__(self, transport: Transport, router: Router) -> None:
        self.transport = transport
        self.queue = JobQueue()
        self._router = router

    # claim a message id in the db, false when already seen
    async def claim(self, message_id: str) -> bool:
        async with SessionFactory() as session:
            session.add(ProcessedMessage(wa_message_id=message_id))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return False
            return True

    # route one claimed message
    async def handle(self, inbound: Inbound) -> None:
        await self._router.handle(inbound, self.transport)

    # drain the queue through the router
    async def process_queued(self) -> None:
        await self.queue.process_all(self.handle)


channel = WhatsAppChannel(WhatsAppTransport(), message_router)
whatsapp_router = APIRouter()


# answer the meta verification challenge
@whatsapp_router.get("/webhook/whatsapp")
async def verify(request: Request) -> PlainTextResponse:
    params = request.query_params
    mode_ok = params.get("hub.mode") == "subscribe"
    token_ok = params.get("hub.verify_token") == settings.whatsapp_verify_token
    if mode_ok and token_ok:
        return PlainTextResponse(params.get("hub.challenge", ""))
    return PlainTextResponse("forbidden", status_code=403)


# accept one signed webhook delivery and queue its messages
@whatsapp_router.post("/webhook/whatsapp")
async def webhook(request: Request, background: BackgroundTasks) -> dict[str, bool]:
    body = await request.body()
    header = request.headers.get("x-hub-signature-256")
    if not verify_signature(body, header, settings.whatsapp_app_secret):
        raise HTTPException(status_code=403, detail="bad signature")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return {"ok": True}
    for inbound in parse_inbound(payload):
        if await channel.claim(inbound.message_id):
            await channel.queue.enqueue(inbound)
    background.add_task(channel.process_queued)
    return {"ok": True}
