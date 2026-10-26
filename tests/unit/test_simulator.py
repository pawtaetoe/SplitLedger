from fastapi.testclient import TestClient

from splitledger.channels.simulator import simulator
from splitledger.main import app


# posting text to the simulator echoes it back through transport
def test_simulator_text_echo():
    simulator.reset()
    client = TestClient(app)
    response = client.post("/dev/chat/text", json={"user": "alice", "text": "hello"})
    assert response.status_code == 200
    messages = response.json()["messages"]
    assert messages[-1] == {"kind": "text", "to": "alice", "text": "hello"}
    assert simulator.transport_for("alice").sent == messages
    assert simulator.transport_for("bob").sent == []


# the chat page is served in dev
def test_simulator_page_served():
    client = TestClient(app)
    response = client.get("/dev/chat?user=alice")
    assert response.status_code == 200
    assert "SplitLedger simulator" in response.text


# posting an image echoes the same bytes back
def test_simulator_image_echo():
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
