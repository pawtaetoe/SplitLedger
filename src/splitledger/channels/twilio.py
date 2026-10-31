import base64
import hashlib
import hmac
import json
import logging
import uuid
from dataclasses import replace
from urllib.parse import parse_qsl

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response

from splitledger.channels.layout import fit, link_key, numbered, split_choice, with_descriptions
from splitledger.channels.whatsapp import WhatsAppChannel
from splitledger.contracts.messages import Inbound
from splitledger.handlers.router import message_router
from splitledger.settings import settings

LOG = logging.getLogger(__name__)
API_BASE = "https://api.twilio.com/2010-04-01/Accounts"
CONTENT_URL = "https://content.twilio.com/v1/Content"
# choices a whatsapp list holds, more are numbered
MAX_LIST = 10
# button id prefix for picking a dish on a bill, ending in the dish number
DISH = "claim:pick:"


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
    parsed = _parse_kind(params, user_id, message_id, body)
    name = params.get("ProfileName") or None
    return replace(parsed, name=name) if parsed is not None else None


# the message by kind, button, image, audio or text
def _parse_kind(
    params: dict[str, str], user_id: str, message_id: str, body: str | None
) -> Inbound | None:
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
        # files like pdfs come as application types, the rest by their first part like video
        kind = "document" if media_type.startswith("application/") else media_type.split("/")[0]
        return Inbound(message_id, user_id, "other", text=kind)
    return Inbound(message_id, user_id, "text", text=body)


# a numbered template variable
def var(n: int) -> str:
    return "{{" + str(n) + "}}"


# the content types for a reply button or list template with numbered variables
def template_types(kind: str, count: int) -> dict[str, object]:
    if kind == "list":
        items = [
            {"item": var(3 * i + 2), "id": var(3 * i + 3), "description": var(3 * i + 4)}
            for i in range(count)
        ]
        return {"twilio/list-picker": {"body": var(1), "button": "Choose", "items": items}}
    actions = [{"title": var(2 * i + 2), "id": var(2 * i + 3)} for i in range(count)]
    return {"twilio/quick-reply": {"body": var(1), "actions": actions}}


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
        self._templates: dict[str, str] = {}
        self._links: dict[str, str] = {}

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
        # dish buttons skip digit menus so a typed number means that dish on the bill
        dishes = all(button_id.startswith(DISH) for button_id, _ in buttons)
        if dishes:
            self._menus.pop(to, None)
        else:
            # digits keep working even when real buttons are shown
            self._menus[to] = {str(n): button_id for n, (button_id, _) in enumerate(buttons, 1)}
        try:
            if len(buttons) <= 3:
                await self._send_quick_reply(to, with_descriptions(text, buttons), buttons)
                return
            if len(buttons) <= MAX_LIST:
                await self._send_list(to, text, buttons)
                return
        except (httpx.HTTPError, RuntimeError, KeyError, ValueError) as error:
            LOG.warning("twilio buttons failed with %s, sending numbers", type(error).__name__)
        # the bill text already numbers its dishes
        await self.send_text(to, text if dishes else numbered(text, buttons))

    # real tappable buttons through a reusable content template
    async def _send_quick_reply(
        self, to: str, body: str, buttons: list[tuple[str, str]]
    ) -> None:
        values = {"1": body}
        for index, (button_id, title) in enumerate(buttons):
            values[str(2 * index + 2)] = fit(split_choice(title)[0])
            values[str(2 * index + 3)] = button_id
        sid = await self._template_sid(len(buttons))
        await self._post_message(to, {"ContentSid": sid, "ContentVariables": json.dumps(values)})

    # four to ten choices as a tap list, each with its id and description
    async def _send_list(self, to: str, body: str, buttons: list[tuple[str, str]]) -> None:
        values = {"1": body}
        for index, (button_id, title) in enumerate(buttons):
            label, description = split_choice(title)
            values[str(3 * index + 2)] = fit(label, row=True)
            values[str(3 * index + 3)] = button_id
            values[str(3 * index + 4)] = (description or label)[:72]
        sid = await self._template_sid(len(buttons), "list")
        await self._post_message(to, {"ContentSid": sid, "ContentVariables": json.dumps(values)})

    # one template per kind and button count, found or created once
    async def _template_sid(self, count: int, kind: str = "buttons") -> str:
        name = f"splitledger_{kind}_{count}"
        if name in self._templates:
            return self._templates[name]
        auth = (self._sid, self._token)
        async with httpx.AsyncClient(timeout=10) as client:
            found = await client.get(CONTENT_URL, params={"PageSize": 100}, auth=auth)
            found.raise_for_status()
            existing = [c["sid"] for c in found.json().get("contents", [])
                        if c.get("friendly_name") == name]
            if existing:
                sid = str(existing[0])
            else:
                content = {
                    "friendly_name": name, "language": "en", "types": template_types(kind, count)
                }
                width = 3 if kind == "list" else 2
                content["variables"] = {str(n): f"value {n}" for n in range(1, width * count + 2)}
                created = await client.post(CONTENT_URL, json=content, auth=auth)
                created.raise_for_status()
                sid = str(created.json()["sid"])
        self._templates[name] = sid
        return sid

    # text with one link button, the address goes through /l/ as plain letters and digits
    async def send_link(self, to: str, text: str, label: str, url: str) -> None:
        base = settings.public_base_url.rstrip("/")
        key = link_key(url.removeprefix(base + "/"))
        try:
            if not url.startswith(base + "/"):
                raise ValueError("link is not on this app")
            sid = await self._link_sid(fit(label), base)
            values = json.dumps({"1": text, "2": key})
            await self._post_message(to, {"ContentSid": sid, "ContentVariables": values})
        except (httpx.HTTPError, RuntimeError, KeyError, ValueError) as error:
            LOG.warning("twilio link button failed with %s, sending the url", type(error).__name__)
            await self.send_text(to, f"{text}\n{url}")

    # one call to action template per button label and app address, found or created once
    async def _link_sid(self, label: str, base: str) -> str:
        digest = hashlib.sha256(f"{label}|{base}".encode()).hexdigest()[:10]
        name = f"splitledger_link_{digest}"
        if name in self._links:
            return self._links[name]
        auth = (self._sid, self._token)
        async with httpx.AsyncClient(timeout=10) as client:
            found = await client.get(CONTENT_URL, params={"PageSize": 100}, auth=auth)
            found.raise_for_status()
            existing = [c["sid"] for c in found.json().get("contents", [])
                        if c.get("friendly_name") == name]
            if existing:
                sid = str(existing[0])
            else:
                action = {"type": "URL", "title": label, "url": f"{base}/l/{{{{2}}}}"}
                content = {
                    "friendly_name": name,
                    "language": "en",
                    "variables": {"1": "body", "2": link_key("d/sample")},
                    "types": {"twilio/call-to-action": {"body": "{{1}}", "actions": [action]}}
                }
                created = await client.post(CONTENT_URL, json=content, auth=auth)
                created.raise_for_status()
                sid = str(created.json()["sid"])
        self._links[name] = sid
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


# accept one signed twilio delivery and route its message after replying
@twilio_router.post("/webhook/twilio")
async def webhook(request: Request, background: BackgroundTasks) -> Response:
    params = dict(parse_qsl((await request.body()).decode(), keep_blank_values=True))
    url = settings.public_base_url.rstrip("/") + "/webhook/twilio"
    signature = request.headers.get("x-twilio-signature")
    if not verify_signature(url, params, signature, settings.twilio_auth_token):
        raise HTTPException(status_code=403, detail="bad signature")
    inbound = parse_inbound(params)
    if inbound is not None and await channel.claim(inbound.message_id):
        background.add_task(channel.handle, transport.apply_menu(inbound))
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
