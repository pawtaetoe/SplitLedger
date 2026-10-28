import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from splitledger.channels.layout import split_choice, with_descriptions
from splitledger.contracts.messages import Inbound
from splitledger.handlers.router import Router, message_router

TEMPLATE = Path(__file__).resolve().parents[1] / "web" / "templates" / "sim.html"


# text posted from the chat page
class TextIn(BaseModel):
    user: str
    text: str


# button tap posted from the chat page
class ButtonIn(BaseModel):
    user: str
    button_id: str


# in memory transport for one simulator user
class SimulatorTransport:
    # share the media store with the simulator
    def __init__(self, media: dict[str, bytes]) -> None:
        self._media = media
        self.sent: list[dict[str, object]] = []

    # record one text
    async def send_text(self, to: str, text: str) -> None:
        self.sent.append({"kind": "text", "to": to, "text": text})

    # record one button message
    async def send_buttons(
        self, to: str, text: str, buttons: list[tuple[str, str]]
    ) -> None:
        shown = [{"id": button_id, "title": split_choice(title)[0]} for button_id, title in buttons]
        body = with_descriptions(text, buttons)
        self.sent.append({"kind": "buttons", "to": to, "text": body, "buttons": shown})

    # record one image and keep its bytes fetchable
    async def send_image(self, to: str, png: bytes, caption: str | None = None) -> None:
        ref = uuid.uuid4().hex
        self._media[ref] = png
        self.sent.append({"kind": "image", "to": to, "caption": caption, "media_ref": ref})

    # return stored media bytes
    async def fetch_media(self, media_ref: str) -> bytes:
        return self._media[media_ref]


# registry of per user simulator transports
class Simulator:
    # start empty with one shared media store
    def __init__(self, router: Router) -> None:
        self._media: dict[str, bytes] = {}
        self._transports: dict[str, SimulatorTransport] = {}
        self.router = router

    # clear all state, for tests
    def reset(self) -> None:
        self._media.clear()
        self._transports.clear()

    # get or create the transport for one user
    def transport_for(self, user: str) -> SimulatorTransport:
        if user not in self._transports:
            self._transports[user] = SimulatorTransport(self._media)
        return self._transports[user]

    # store uploaded bytes and return their reference
    def store_upload(self, data: bytes) -> str:
        ref = uuid.uuid4().hex
        self._media[ref] = data
        return ref

    # look up stored media bytes
    def media_bytes(self, ref: str) -> bytes:
        return self._media[ref]

    # hand one inbound message to the router
    async def receive(self, inbound: Inbound) -> list[dict[str, object]]:
        transport = self.transport_for(inbound.user_id)
        await self.router.handle(inbound, transport)
        return transport.sent


simulator = Simulator(message_router)
dev_router = APIRouter()


# serve the chat page
@dev_router.get("/dev/chat")
async def chat_page() -> HTMLResponse:
    return HTMLResponse(TEMPLATE.read_text(encoding="utf-8"))


# accept one text message and return the conversation
@dev_router.post("/dev/chat/text")
async def chat_text(payload: TextIn) -> dict[str, object]:
    if not payload.user.strip() or not payload.text.strip():
        raise HTTPException(status_code=400, detail="user and text are required")
    inbound = Inbound(
        message_id=uuid.uuid4().hex, user_id=payload.user, kind="text", text=payload.text
    )
    return {"messages": await simulator.receive(inbound)}


# accept one button tap and return the conversation
@dev_router.post("/dev/chat/button")
async def chat_button(payload: ButtonIn) -> dict[str, object]:
    inbound = Inbound(
        message_id=uuid.uuid4().hex,
        user_id=payload.user,
        kind="button",
        button_id=payload.button_id
    )
    return {"messages": await simulator.receive(inbound)}


# accept one image or audio upload and return the conversation
@dev_router.post("/dev/chat/upload")
async def chat_upload(request: Request, user: str, kind: str) -> dict[str, object]:
    if kind not in ("image", "audio"):
        raise HTTPException(status_code=400, detail="kind must be image or audio")
    ref = simulator.store_upload(await request.body())
    media_kind: Literal["image", "audio"] = "image" if kind == "image" else "audio"
    inbound = Inbound(
        message_id=uuid.uuid4().hex, user_id=user, kind=media_kind, media_ref=ref
    )
    return {"messages": await simulator.receive(inbound)}


# serve stored media bytes back to the page
@dev_router.get("/dev/chat/media/{ref}")
async def chat_media(ref: str) -> Response:
    try:
        data = simulator.media_bytes(ref)
    except KeyError:
        raise HTTPException(status_code=404, detail="media not found") from None
    return Response(content=data, media_type="application/octet-stream")


# conversation history for one user
@dev_router.get("/dev/chat/history")
async def chat_history(user: str) -> dict[str, object]:
    return {"messages": simulator.transport_for(user).sent}
