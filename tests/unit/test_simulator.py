from fastapi.testclient import TestClient

from splitledger.channels.simulator import simulator
from splitledger.handlers.router import MessageReader, Router
from splitledger.llm.schemas import GLOBAL
from splitledger.main import app
from tests.fakes import FakeAssistant, FakeLLM, FakeLogger, FakePlans


# posting text reaches the router and replies through transport
def test_simulator_text_echo(monkeypatch):
    router = Router(FakeLogger(), MessageReader(FakeLLM(), GLOBAL), FakeAssistant(), FakePlans())
    monkeypatch.setattr(simulator, "router", router)
    simulator.reset()
    client = TestClient(app)
    response = client.post("/dev/chat/text", json={"user": "alice", "text": "coffee 6.50"})
    assert response.status_code == 200
    messages = response.json()["messages"]
    assert messages[-1] == {"kind": "text", "to": "alice", "text": "coffee 6.50"}
    assert simulator.transport_for("alice").sent == messages
    assert simulator.transport_for("bob").sent == []


# the chat page is served in dev
def test_simulator_page_served():
    client = TestClient(app)
    response = client.get("/dev/chat?user=alice")
    assert response.status_code == 200
    assert "SplitLedger simulator" in response.text


# posting an image reaches the router and the same bytes come back
def test_simulator_image_echo(monkeypatch):
    router = Router(FakeLogger(), MessageReader(FakeLLM(), GLOBAL), FakeAssistant(), FakePlans())
    monkeypatch.setattr(simulator, "router", router)
    simulator.reset()
    client = TestClient(app)
    response = client.post(
        "/dev/chat/upload?user=alice&kind=image", content=b"fake-png"
    )
    assert response.status_code == 200
    message = response.json()["messages"][-1]
    assert message["kind"] == "image"
    media = client.get(f"/dev/chat/media/{message['media_ref']}")
    assert media.content == b"fake-png"


# clicking a button echoes its id
def test_simulator_button_echo():
    simulator.reset()
    client = TestClient(app)
    response = client.post(
        "/dev/chat/button", json={"user": "alice", "button_id": "save"}
    )
    assert response.status_code == 200
    assert response.json()["messages"][-1]["text"] == "save"
