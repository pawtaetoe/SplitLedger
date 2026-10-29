import uuid
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from splitledger.channels.layout import link_key
from splitledger.handlers.dashboard import bill_links
from splitledger.split.tokens import ClaimTokens, LoginLinks
from splitledger.web import dashboard as dashboard_module
from splitledger.web.dashboard import DashboardPage, dashboard_router

NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)


# an overview that must never be reached
class NoOverview:
    # fail loudly if a bad sign in got through
    async def build(self, user_id):
        raise AssertionError("bad sign ins must not reach the database")


# a link works until it expires, any change breaks it, and a claim cookie is never a login
def test_login_links():
    tokens = ClaimTokens("secret")
    links = LoginLinks(tokens, 15 * 60)
    user = str(uuid.uuid4())
    link = links.make(user, NOW)
    assert links.read(link, NOW + timedelta(minutes=14)) == user
    assert links.read(link, NOW + timedelta(minutes=16)) is None
    assert links.read(link.replace(user[:4], "ffff"), NOW) is None
    assert links.read(tokens.seal(user), NOW) is None
    assert LoginLinks(ClaimTokens("other"), 900).read(link, NOW) is None
    assert LoginLinks(ClaimTokens(""), 900).read(link, NOW) is None


# a bad link and a missing cookie explain how to get in, without touching the database
def test_sign_in_pages(monkeypatch):
    page = DashboardPage(ClaimTokens("secret"), NoOverview(), False, "6512345678")
    monkeypatch.setattr(dashboard_module, "dashboard_page", page)
    app = FastAPI()
    app.include_router(dashboard_router)
    web = TestClient(app)
    an_hour_ago = datetime.now(UTC) - timedelta(hours=1)
    expired = LoginLinks(ClaimTokens("secret"), 900).make(str(uuid.uuid4()), an_hour_ago)
    response = web.get(f"/d/{expired}", follow_redirects=False)
    assert response.status_code == 401 and "This link has expired" in response.text
    old_bill = web.get(f"/d/{expired}?to=bill-K7Q2AB", follow_redirects=False)
    assert "wa.me/6512345678?text=CLAIM%20K7Q2AB" in old_bill.text
    response = web.get("/me")
    assert response.status_code == 401 and "Open your dashboard from WhatsApp" in response.text
    paid = web.post("/me/paid", data={"to": str(uuid.uuid4()), "currency": "SGD"})
    assert paid.status_code == 401
    assert "https://wa.me/" in response.text and "Get a link" in response.text
    fresh = LoginLinks(ClaimTokens("secret"), 900).make(str(uuid.uuid4()), datetime.now(UTC))
    response = web.get(f"/d/{fresh}", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/me"
    assert "sl_me=" in response.headers["set-cookie"]
    to_plan = web.get(f"/d/{fresh}?to=plan", follow_redirects=False)
    assert to_plan.headers["location"] == "/plan"
    elsewhere = web.get(f"/d/{fresh}?to=https://evil.example", follow_redirects=False)
    assert elsewhere.headers["location"] == "/me"
    to_bill = web.get(f"/d/{fresh}?to=bill-k7q2ab", follow_redirects=False)
    code = ClaimTokens("secret").read(to_bill.headers["location"].removeprefix("/c/"), NOW)
    assert code == "K7Q2AB"
    # once this phone is signed in an old link still opens its page
    again = web.get(f"/d/{expired}?to=bill-K7Q2AB", follow_redirects=False)
    assert again.status_code == 303 and again.headers["location"].startswith("/c/")



# a link button lands on its page with the query kept, anything else goes to the dashboard
def test_link_buttons():
    app = FastAPI()
    app.include_router(dashboard_router)
    web = TestClient(app)
    went = web.get(f"/l/{link_key('b/K7Q2AB/bill-K7Q2AB:u.1.s')}", follow_redirects=False)
    assert went.status_code == 303 and went.headers["location"] == "/b/K7Q2AB/bill-K7Q2AB:u.1.s"
    plan = web.get(f"/l/{link_key('d/t.1.s?to=plan')}", follow_redirects=False)
    assert plan.headers["location"] == "/d/t.1.s?to=plan"
    for bad in ["x/evil", "//evil.example", "d//evil.example"]:
        assert web.get(f"/l/{link_key(bad)}", follow_redirects=False).headers["location"] == "/me"
    assert web.get("/l/not-base64!", follow_redirects=False).headers["location"] == "/me"
    assert web.get("/", follow_redirects=False).headers["location"] == "/me"


# a bill link opens only its own bill for two weeks, never the dashboard
def test_bill_links_are_scoped():
    tokens = ClaimTokens("secret")
    user = str(uuid.uuid4())
    link = bill_links(tokens, "k7q2ab").make(user, NOW)
    assert bill_links(tokens, "K7Q2AB").read(link, NOW + timedelta(days=13)) == user
    assert bill_links(tokens, "K7Q2AB").read(link, NOW + timedelta(days=15)) is None
    assert bill_links(tokens, "ZZZZZZ").read(link, NOW) is None
    assert LoginLinks(tokens, 900).read(link, NOW) is None
