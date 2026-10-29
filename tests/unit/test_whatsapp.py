import hashlib
import hmac
import json
from pathlib import Path

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

import splitledger.channels.whatsapp as whatsapp
from splitledger.settings import settings
from tests.fakes import FakeTransport
from tests.fakes.router import fake_router

FIXTURES = Path(__file__).parents[1] / "fixtures" / "whatsapp"
SECRET = "test-secret"
app = FastAPI()
app.include_router(whatsapp.whatsapp_router)


# load one fixture payload
def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# sign a body the way meta does
def sign(body: bytes) -> str:
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


# each fixture type parses into the right inbound kind
def test_parse_fixtures():
    text = whatsapp.parse_inbound(load("text.json"))[0]
    assert (text.kind, text.text, text.user_id, text.message_id) == (
        "text", "Coffee $6.50", "6581234567", "wamid.text1"
    )
    assert text.name == "Alice"
    image = whatsapp.parse_inbound(load("image.json"))[0]
    assert (image.kind, image.media_ref, image.text) == ("image", "media-image-1", "split this")
    audio = whatsapp.parse_inbound(load("audio.json"))[0]
    assert (audio.kind, audio.media_ref) == ("audio", "media-audio-1")
    button = whatsapp.parse_inbound(load("interactive.json"))[0]
    assert (button.kind, button.button_id) == ("button", "save")


# signature check accepts meta style headers only
def test_verify_signature():
    body = b"payload"
    assert whatsapp.verify_signature(body, sign(body), SECRET)
    assert not whatsapp.verify_signature(body, "sha256=deadbeef", SECRET)
    assert not whatsapp.verify_signature(body, None, SECRET)


# swap in a fake channel with in memory dedupe
def use_fake_channel(monkeypatch) -> FakeTransport:
    fake = FakeTransport()
    channel = whatsapp.WhatsAppChannel(fake, fake_router())
    seen: set[str] = set()

    async def claim(message_id: str) -> bool:
        if message_id in seen:
            return False
        seen.add(message_id)
        return True

    channel.claim = claim
    monkeypatch.setattr(whatsapp, "channel", channel)
    monkeypatch.setattr(settings, "whatsapp_app_secret", SECRET)
    return fake


# a validly signed message is echoed and its replay is skipped
def test_webhook_valid_then_replay(monkeypatch):
    fake = use_fake_channel(monkeypatch)
    client = TestClient(app)
    body = json.dumps(load("text.json")).encode()
    headers = {"X-Hub-Signature-256": sign(body), "Content-Type": "application/json"}
    first = client.post("/webhook/whatsapp", content=body, headers=headers)
    assert first.status_code == 200
    assert fake.sent_text == [("6581234567", "Coffee $6.50")]
    second = client.post("/webhook/whatsapp", content=body, headers=headers)
    assert second.status_code == 200
    assert fake.sent_text == [("6581234567", "Coffee $6.50")]


# a bad signature is rejected and nothing is sent
def test_webhook_bad_signature(monkeypatch):
    fake = use_fake_channel(monkeypatch)
    client = TestClient(app)
    body = json.dumps(load("text.json")).encode()
    response = client.post(
        "/webhook/whatsapp",
        content=body,
        headers={"X-Hub-Signature-256": "sha256=deadbeef"}
    )
    assert response.status_code == 403
    assert fake.sent_text == []


# outbound text goes to the graph api
@respx.mock
async def test_send_text_calls_graph_api():
    route = respx.post("https://graph.facebook.com/v21.0/999/messages").mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.out"}]})
    )
    transport = whatsapp.WhatsAppTransport(access_token="token", phone_number_id="999")
    await transport.send_text("6581234567", "hello")
    payload = json.loads(route.calls.last.request.content)
    assert payload["to"] == "6581234567"
    assert payload["text"] == {"body": "hello"}
    assert route.calls.last.request.headers["Authorization"] == "Bearer token"


# a graph error surfaces its code and message
@respx.mock
async def test_send_text_surfaces_graph_error():
    respx.post("https://graph.facebook.com/v21.0/999/messages").mock(
        return_value=httpx.Response(
            400,
            json={"error": {"message": "recipient not allowed", "code": 131030}}
        )
    )
    transport = whatsapp.WhatsAppTransport(access_token="token", phone_number_id="999")
    with pytest.raises(RuntimeError, match="131030"):
        await transport.send_text("6581234567", "hello")


# a link becomes one button that opens it
@respx.mock
async def test_send_link_is_a_button():
    route = respx.post("https://graph.facebook.com/v21.0/999/messages").mock(
        return_value=httpx.Response(200, json={"messages": [{"id": "wamid.out"}]})
    )
    transport = whatsapp.WhatsAppTransport(access_token="token", phone_number_id="999")
    await transport.send_link("6581234567", "*Your dashboard*", "Open dashboard", "https://x/d/t")
    interactive = json.loads(route.calls.last.request.content)["interactive"]
    assert interactive["type"] == "cta_url"
    assert interactive["action"]["parameters"] == {
        "display_text": "Open dashboard", "url": "https://x/d/t"
    }
