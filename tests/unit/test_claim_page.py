import uuid
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from splitledger.db.models import Bill, BillItem, ItemClaim
from splitledger.split.claims import preview_share
from splitledger.split.tokens import ClaimTokens
from splitledger.web import claim as claim_module
from splitledger.web.claim import ClaimPage, claim_router


# a claims store that must never be reached
class NoBook:
    # fail loudly if a bad link got through
    async def view(self, code, me_id):
        raise AssertionError("bad links must not reach the database")


# a client for the claim routes with a known secret
def client(monkeypatch) -> TestClient:
    page = ClaimPage(ClaimTokens("secret"), NoBook(), "6512345678")
    monkeypatch.setattr(claim_module, "claim_page", page)
    app = FastAPI()
    app.include_router(claim_router)
    return TestClient(app)


# a wrong, edited or expired link gets a friendly page and never touches the database
def test_bad_links(monkeypatch):
    web = client(monkeypatch)
    old = ClaimTokens("secret").make("K7Q2AB", datetime.now(UTC) - timedelta(days=30))
    edited = ClaimTokens("secret").make("K7Q2AB", datetime.now(UTC)).replace("K7Q2AB", "K7Q2AC")
    for token in ["nonsense", old, edited]:
        response = web.get(f"/c/{token}")
        assert response.status_code == 404
        assert "This link isn't working" in response.text


# a cookie only names a person when its signature holds
def test_cookie_seal():
    tokens = ClaimTokens("secret")
    person = str(uuid.uuid4())
    assert tokens.unseal(tokens.seal(person)) == person
    assert tokens.unseal(tokens.seal(person).replace(person[:4], "ffff")) is None
    assert tokens.unseal("nonsense") is None


# four pastas and a calamari two people shared, with service and gst on top
def test_preview_share_shared_dish():
    bill = Bill(subtotal=9800, total=11750)
    pastas = [BillItem(id=uuid.uuid4(), price_cents=2000) for _ in range(4)]
    calamari = BillItem(id=uuid.uuid4(), price_cents=1800)
    anna, ben, cara = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    claims = [
        ItemClaim(bill_item_id=pastas[0].id, participant_id=anna, weight=1.0),
        ItemClaim(bill_item_id=pastas[1].id, participant_id=ben, weight=1.0),
        ItemClaim(bill_item_id=pastas[2].id, participant_id=cara, weight=1.0),
        ItemClaim(bill_item_id=calamari.id, participant_id=anna, weight=1.0),
        ItemClaim(bill_item_id=calamari.id, participant_id=ben, weight=1.0)
    ]
    items = [*pastas, calamari]
    assert preview_share(bill, items, claims, anna) == 3477
    assert preview_share(bill, items, claims, cara) == 2398
