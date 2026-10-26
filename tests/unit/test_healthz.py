from fastapi.testclient import TestClient

from splitledger.main import app


# healthz reports ok
def test_healthz_returns_200():
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
