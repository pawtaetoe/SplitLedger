import base64
import hashlib
import hmac
import json
import logging
import uuid
from urllib.parse import parse_qsl

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response

from splitledger.channels.layout import numbered, split_choice, with_descriptions
from splitledger.channels.whatsapp import WhatsAppChannel
from splitledger.contracts.messages import Inbound
from splitledger.handlers.router import message_router
from splitledger.settings import settings

LOG = logging.getLogger(__name__)
API_BASE = "https://api.twilio.com/2010-04-01/Accounts"
CONTENT_URL = "https://content.twilio.com/v1/Content"


# check a twilio signature against the url and posted params
def verify_signature(url: str, params: dict[str, str], header: str | None, token: str) -> bool:
    if not header or not token:
        return False
    signed = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    digest = hmac.new(token.encode(), signed.encode(), hashlib.sha1).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), header)


# turn one posted form into an inbound message
def parse_inbound(params: dict[str, str]) -> Inbound | None:
    user_id = params.get("WaId") or params.get("From", "").removeprefix("whatsapp:+")
    message_id = params.get("MessageSid", "")
    if not user_id or not message_id:
        return None
    body = params.get("Body") or None
    if params.get("ButtonPayload"):
        return Inbound(
            message_id, user_id, "button", text=params.get("ButtonText"),
            button_id=params["ButtonPayload"]
        )
    media_count = params.get("NumMedia", "0")
    if media_count.isdigit() and int(media_count) > 0:
        media_type = params.get("MediaContentType0", "")
        media_ref = params.get("MediaUrl0")
        if media_type.startswith("image/"):
            return Inbound(message_id, user_id, "image", text=body, media_ref=media_ref)
        if media_type.startswith("audio/"):
            return Inbound(message_id, user_id, "audio", media_ref=media_ref)
        return None
    return Inbound(message_id, user_id, "text", text=body)


# twilio sandbox transport, buttons become numbered options
class TwilioTransport:
    # credentials default from settings
    def __init__(
        self, account_sid: str | None = None, auth_token: str | None = None,
        sender: str | None = None
    ) -> None:
        self._sid = account_sid or settings.twilio_account_sid
        self._token = auth_token or settings.twilio_auth_token
        self._from = sender or settings.twilio_whatsapp_from
        self._media: dict[str, bytes] = {}
        self._menus: dict[str, dict[str, str]] = {}
        self._templates: dict[int, str] = {}

    # post one message form to twilio
    async def _post_message(self, to: str, fields: dict[str, str]) -> None:
        form = {"From": self._from, "To": f"whatsapp:+{to}", **fields}
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{API_BASE}/{self._sid}/Messages.json", data=form, auth=(self._sid, self._token)
            )
        if response.is_error:
            error = response.json()
            raise RuntimeError(f"twilio {error.get('code')} {error.get('message')}")

    # send one text message
    async def send_text(self, to: str, text: str) -> None:
        await self._post_message(to, {"Body": text})

    # send text plus numbered options and remember them for the next reply
    async def send_buttons(self, to: str, text: str, buttons: list[tuple[str, str]]) -> None:
        # digits keep working even when real buttons are shown
        self._menus[to] = {str(n): button_id for n, (button_id, _) in enumerate(buttons, 1)}
        if len(buttons) <= 3:
            try:
                await self._send_quick_reply(to, with_descriptions(text, buttons), buttons)
                return
            except (httpx.HTTPError, RuntimeError, KeyError, ValueError) as error:
                LOG.warning("twilio buttons failed with %s, sending numbers", type(error).__name__)
        await self.send_text(to, numbered(text, buttons))

    # real tappable buttons through a reusable content template
    async def _send_quick_reply(
        self, to: str, body: str, buttons: list[tuple[str, str]]
    ) -> None:
        values = {"1": body}
        for index, (button_id, title) in enumerate(buttons):
            values[str(2 * index + 2)] = split_choice(title)[0]
            values[str(2 * index + 3)] = button_id
        sid = await self._template_sid(len(buttons))
        await self._post_message(to, {"ContentSid": sid, "ContentVariables": json.dumps(values)})

    # one quick reply template per button count, found or created once
    async def _template_sid(self, count: int) -> str:
        if count in self._templates:
            return self._templates[count]
        name = f"splitledger_buttons_{count}"
        auth = (self._sid, self._token)
        async with httpx.AsyncClient(timeout=10) as client:
            found = await client.get(CONTENT_URL, params={"PageSize": 100}, auth=auth)
            found.raise_for_status()
            existing = [c["sid"] for c in found.json().get("contents", [])
                        if c.get("friendly_name") == name]
            if existing:
                sid = str(existing[0])
            else:
                actions = [{"title": f"{{{{{2 * i + 2}}}}}", "id": f"{{{{{2 * i + 3}}}}}"}
                           for i in range(count)]
                variables = {str(n): f"value {n}" for n in range(1, 2 * count + 2)}
                content = {
                    "friendly_name": name,
                    "language": "en",
                    "variables": variables,
                    "types": {"twilio/quick-reply": {"body": "{{1}}", "actions": actions}}
                }
                created = await client.post(CONTENT_URL, json=content, auth=auth)
                created.raise_for_status()
                sid = str(created.json()["sid"])
        self._templates[count] = sid
        return sid

    # keep the image and send twilio a public link to it
    async def send_image(self, to: str, png: bytes, caption: str | None = None) -> None:
        ref = uuid.uuid4().hex
        self._media[ref] = png
        link = f"{settings.public_base_url.rstrip('/')}/media/twilio/{ref}"
        await self._post_message(to, {"Body": caption or "", "MediaUrl": link})

    # download media from twilio with basic auth
    async def fetch_media(self, media_ref: str) -> bytes:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            response = await client.get(media_ref, auth=(self._sid, self._token))
            response.raise_for_status()
            return response.content

    # stored image bytes for the media route
    def media_bytes(self, ref: str) -> bytes:
        return self._media[ref]

    # turn a digit reply into a button, any other message clears the menu
    def apply_menu(self, inbound: Inbound) -> Inbound:
        menu = self._menus.pop(inbound.user_id, None)
        choice = (inbound.text or "").strip()
        if menu and inbound.kind == "text" and choice in menu:
            return Inbound(
                inbound.message_id, inbound.user_id, "button", text=choice,
                button_id=menu[choice]
            )
        return inbound


transport = TwilioTransport()
channel = WhatsAppChannel(transport, message_router)
twilio_router = APIRouter()


# accept one signed twilio delivery and queue its message
@twilio_router.post("/webhook/twilio")
async def webhook(request: Request, background: BackgroundTasks) -> Response:
    params = dict(parse_qsl((await request.body()).decode(), keep_blank_values=True))
    url = settings.public_base_url.rstrip("/") + "/webhook/twilio"
    signature = request.headers.get("x-twilio-signature")
    if not verify_signature(url, params, signature, settings.twilio_auth_token):
        raise HTTPException(status_code=403, detail="bad signature")
    inbound = parse_inbound(params)
    if inbound is not None and await channel.claim(inbound.message_id):
        await channel.queue.enqueue(transport.apply_menu(inbound))
        background.add_task(channel.process_queued)
    return Response("<Response/>", media_type="application/xml")


# serve a stored image to twilio
@twilio_router.get("/media/twilio/{ref}")
async def media(ref: str) -> Response:
    try:
        data = transport.media_bytes(ref)
    except KeyError:
        raise HTTPException(status_code=404, detail="media not found") from None
    # echoed photos are jpeg so read the type from the first bytes
    media_type = "image/jpeg" if data.startswith(b"\xff\xd8") else "image/png"
    return Response(content=data, media_type=media_type)
