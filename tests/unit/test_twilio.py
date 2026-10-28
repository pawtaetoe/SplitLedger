import base64
import hashlib
import hmac
import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

import splitledger.channels.twilio as twilio
from splitledger.channels.whatsapp import WhatsAppChannel
from splitledger.contracts.messages import Inbound
from splitledger.handlers.router import MessageReader, Router
from splitledger.llm.schemas import GLOBAL
from splitledger.settings import settings
from tests.fakes import FakeAssistant, FakeBills, FakeLLM, FakeLogger, FakePlans

FIXTURES = Path(__file__).parents[1] / "fixtures" / "twilio"
TOKEN = "test-token"
SID = "AC123"
BASE = "https://example.ngrok.app"
SENDER = "whatsapp:+14155238886"
MESSAGES_URL = f"https://api.twilio.com/2010-04-01/Accounts/{SID}/Messages.json"
CONTENT_URL = "https://content.twilio.com/v1/Content"


# load one fixture form
def load(name: str) -> dict[str, str]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# sign a form the way twilio does
def sign(params: dict[str, str]) -> str:
    signed = BASE + "/webhook/twilio" + "".join(f"{key}{params[key]}" for key in sorted(params))
    digest = hmac.new(TOKEN.encode(), signed.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


# app with only the twilio routes
def make_client() -> TestClient:
    app = FastAPI()
    app.include_router(twilio.twilio_router)
    return TestClient(app)


# swap in a real twilio transport with in memory dedupe
def use_twilio_channel(monkeypatch) -> twilio.TwilioTransport:
    transport = twilio.TwilioTransport(SID, TOKEN, SENDER)
    reader = MessageReader(FakeLLM(), GLOBAL)
    router = Router(FakeLogger(), reader, FakeAssistant(), FakePlans(), FakeBills())
    channel = WhatsAppChannel(transport, router)
    seen: set[str] = set()

    async def claim(message_id: str) -> bool:
        if message_id in seen:
            return False
        seen.add(message_id)
        return True

    channel.claim = claim
    monkeypatch.setattr(twilio, "transport", transport)
    monkeypatch.setattr(twilio, "channel", channel)
    monkeypatch.setattr(settings, "twilio_auth_token", TOKEN)
    monkeypatch.setattr(settings, "public_base_url", BASE)
    return transport


# each fixture parses into the right inbound kind
def test_parse_fixtures():
    text = twilio.parse_inbound(load("text.json"))
    assert text == Inbound("SMtext1", "6581234567", "text", text="Coffee $6.50")
    image = twilio.parse_inbound(load("image.json"))
    assert image == Inbound(
        "SMimage1", "6581234567", "image", text="split this",
        media_ref="https://api.twilio.com/media/image-1"
    )
    audio = twilio.parse_inbound(load("audio.json"))
    assert audio == Inbound(
        "SMaudio1", "6581234567", "audio", media_ref="https://api.twilio.com/media/audio-1"
    )
    reply = twilio.parse_inbound(load("reply.json"))
    assert reply == Inbound("SMreply1", "6581234567", "button", text="Save", button_id="save")


# a form with no sid or sender is ignored
def test_parse_ignores_incomplete_form():
    assert twilio.parse_inbound({"Body": "hi"}) is None


# signature check accepts the right token only
def test_verify_signature():
    params = load("text.json")
    url = BASE + "/webhook/twilio"
    assert twilio.verify_signature(url, params, sign(params), TOKEN)
    assert not twilio.verify_signature(url, params, sign(params), "other-token")
    assert not twilio.verify_signature(url, params, None, TOKEN)


# a validly signed message is echoed and its replay is skipped
@respx.mock
def test_webhook_valid_then_replay(monkeypatch):
    use_twilio_channel(monkeypatch)
    route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(201, json={"sid": "SM1"}))
    client = make_client()
    params = load("text.json")
    headers = {"X-Twilio-Signature": sign(params)}
    first = client.post("/webhook/twilio", data=params, headers=headers)
    assert first.status_code == 200
    assert first.text == "<Response/>"
    sent = parse_qs(route.calls.last.request.content.decode())
    assert sent["Body"] == ["Coffee $6.50"]
    assert sent["To"] == ["whatsapp:+6581234567"]
    assert sent["From"] == [SENDER]
    second = client.post("/webhook/twilio", data=params, headers=headers)
    assert second.status_code == 200
    assert route.call_count == 1


# a bad signature is rejected and nothing is sent
@respx.mock
def test_webhook_bad_signature(monkeypatch):
    use_twilio_channel(monkeypatch)
    route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(201, json={}))
    response = make_client().post(
        "/webhook/twilio", data=load("text.json"), headers={"X-Twilio-Signature": "bad"}
    )
    assert response.status_code == 403
    assert route.call_count == 0


# real buttons go through a quick reply template created once and reused
@respx.mock
async def test_quick_reply_buttons():
    listed = respx.get(CONTENT_URL).mock(return_value=httpx.Response(200, json={"contents": []}))
    created = respx.post(CONTENT_URL).mock(return_value=httpx.Response(201, json={"sid": "HX1"}))
    route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(201, json={}))
    transport = twilio.TwilioTransport(SID, TOKEN, SENDER)
    buttons = [("plan:fw:503020", "50/30/20 — needs, wants, savings"), ("plan:cancel", "Cancel")]
    await transport.send_buttons("6581234567", "*Pick a plan*", buttons)
    await transport.send_buttons("6581234567", "*Again*", buttons)
    sent = parse_qs(route.calls.last.request.content.decode())
    assert sent["ContentSid"] == ["HX1"]
    values = json.loads(sent["ContentVariables"][0])
    assert values == {
        "1": "*Again*\n\n- *50/30/20* — needs, wants, savings",
        "2": "50/30/20", "3": "plan:fw:503020", "4": "Cancel", "5": "plan:cancel"
    }
    template = json.loads(created.calls.last.request.content)
    actions = template["types"]["twilio/quick-reply"]["actions"]
    assert actions[1] == {"title": "{{4}}", "id": "{{5}}"}
    assert (listed.call_count, created.call_count) == (1, 1)


# when buttons fail the choices are numbered and a digit reply becomes the button
@respx.mock
async def test_digit_reply_becomes_button():
    respx.get(CONTENT_URL).mock(return_value=httpx.Response(500))
    route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(201, json={}))
    transport = twilio.TwilioTransport(SID, TOKEN, SENDER)
    await transport.send_buttons("6581234567", "Logged", [("undo:1", "Undo"), ("keep", "Keep")])
    sent = parse_qs(route.calls.last.request.content.decode())
    assert sent["Body"] == ["Logged\n\n*1* · Undo\n*2* · Keep\n\n_Reply with a number_"]
    reply = Inbound("m1", "6581234567", "text", text="2")
    picked = transport.apply_menu(reply)
    assert (picked.kind, picked.button_id) == ("button", "keep")
    assert transport.apply_menu(reply).kind == "text"


# any other message clears the menu
@respx.mock
async def test_other_message_clears_menu():
    respx.get(CONTENT_URL).mock(return_value=httpx.Response(500))
    respx.post(MESSAGES_URL).mock(return_value=httpx.Response(201, json={}))
    transport = twilio.TwilioTransport(SID, TOKEN, SENDER)
    await transport.send_buttons("6581234567", "Logged", [("undo:1", "Undo")])
    assert transport.apply_menu(Inbound("m1", "6581234567", "text", text="hello")).kind == "text"
    assert transport.apply_menu(Inbound("m2", "6581234567", "text", text="1")).kind == "text"


# a twilio error surfaces its code and message
@respx.mock
async def test_send_text_surfaces_error():
    respx.post(MESSAGES_URL).mock(
        return_value=httpx.Response(400, json={"code": 63015, "message": "not joined"})
    )
    transport = twilio.TwilioTransport(SID, TOKEN, SENDER)
    try:
        await transport.send_text("6581234567", "hello")
    except RuntimeError as error:
        assert "63015" in str(error)
    else:
        raise AssertionError("expected a runtime error")


# media download uses basic auth and follows redirects
@respx.mock
async def test_fetch_media_follows_redirect():
    first = respx.get("https://api.twilio.com/media/image-1").mock(
        return_value=httpx.Response(302, headers={"Location": "https://s3.example/image-1"})
    )
    respx.get("https://s3.example/image-1").mock(return_value=httpx.Response(200, content=b"png"))
    transport = twilio.TwilioTransport(SID, TOKEN, SENDER)
    assert await transport.fetch_media("https://api.twilio.com/media/image-1") == b"png"
    assert first.calls.last.request.headers["Authorization"].startswith("Basic ")


# an image is stored and sent as a public link the media route serves
@respx.mock
async def test_send_image_serves_media(monkeypatch):
    transport = use_twilio_channel(monkeypatch)
    route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(201, json={}))
    await transport.send_image("6581234567", b"pngbytes", caption="hello")
    sent = parse_qs(route.calls.last.request.content.decode())
    link = sent["MediaUrl"][0]
    assert link.startswith(f"{BASE}/media/twilio/")
    served = make_client().get(link.removeprefix(BASE))
    assert (served.status_code, served.content) == (200, b"pngbytes")
    assert make_client().get("/media/twilio/missing").status_code == 404


# a jpeg is served with the jpeg type
@respx.mock
async def test_media_route_detects_jpeg(monkeypatch):
    transport = use_twilio_channel(monkeypatch)
    route = respx.post(MESSAGES_URL).mock(return_value=httpx.Response(201, json={}))
    await transport.send_image("6581234567", b"\xff\xd8jpegbytes")
    link = parse_qs(route.calls.last.request.content.decode())["MediaUrl"][0]
    served = make_client().get(link.removeprefix(BASE))
    assert served.headers["content-type"] == "image/jpeg"
