from sqlalchemy import select

from splitledger.contracts.messages import Inbound
from splitledger.db.models import ItemClaim, Participant
from splitledger.handlers import claim as claim_module
from splitledger.handlers.claim import ClaimChat
from splitledger.llm.schemas import GLOBAL, ClaimMap, ClaimPart, PersonClaims
from splitledger.split.claims import ClaimBook
from tests.fakes import FakeLLM, FakeTransport
from tests.integration.test_claims import seed

PAYER = "6581234567"
ANNA = "6591111111"
BEN = "6592222222"


# one text from someone with a whatsapp profile name
def text(user: str, body: str, name: str | None = None) -> Inbound:
    return Inbound("m1", user, "text", text=body, name=name)


# one button tap
def tap(user: str, button_id: str) -> Inbound:
    return Inbound("m1", user, "button", button_id=button_id)


# bill links sent, by code
class FakeLinks:
    # none yet
    def __init__(self) -> None:
        self.bills: list[str] = []

    # one bill link
    async def bill_url(self, inbound, code: str) -> str:
        self.bills.append(code)
        return f"https://sl.test/b/{code}/token"


# a claim chat with an optional canned reading of plain words
def chat(sessions, reading: ClaimMap | None = None, links=None) -> ClaimChat:
    llm = FakeLLM({"map_claim": reading} if reading else {})
    return ClaimChat(sessions, llm, ClaimBook(sessions), GLOBAL, links)


# a wrong code says so, the code alone lists items with the free ones as buttons
async def test_unknown_code_and_list(sessions):
    await seed(sessions)
    claims = chat(sessions)
    transport = FakeTransport()
    await claims.by_code(text(ANNA, "CLAIM ZZZZZZ"), "CLAIM ZZZZZZ", transport)
    assert transport.sent_text[-1][1] == claim_module.UNKNOWN.format("ZZZZZZ")
    await claims.by_code(text(ANNA, "claim k7q2ab", "Anna"), "claim k7q2ab", transport)
    _, body, buttons = transport.sent_buttons[-1]
    assert body.startswith("*Bill · code K7Q2AB*\n1. Pasta · SGD 20.00")
    assert buttons[2] == ("claim:pick:K7Q2AB:3", "3. Calamari")


# numbers claim items, a taken one offers a share button that joins it
async def test_claim_by_numbers(sessions):
    await seed(sessions)
    claims = chat(sessions)
    transport = FakeTransport()
    await claims.by_code(text(ANNA, "CLAIM K7Q2AB 3", "Anna"), "CLAIM K7Q2AB 3", transport)
    assert transport.sent_text[-1][1].startswith("*Your share so far · SGD ")
    await claims.by_code(text(BEN, "CLAIM K7Q2AB 1,3", "Ben"), "CLAIM K7Q2AB 1,3", transport)
    _, body, buttons = transport.sent_buttons[-1]
    assert "Calamari is taken by Anna" in body
    assert buttons == [("claim:share:K7Q2AB:3", "I shared Calamari")]
    await claims.on_button(tap(BEN, buttons[0][0]), buttons[0][0], transport)
    async with sessions() as session:
        count = len((await session.execute(select(ItemClaim))).all())
    assert count == 3


# a guest who joined on the web is linked when they claim from whatsapp, never doubled
async def test_linking_backfills_user(sessions):
    bill, _, _ = await seed(sessions)
    book = ClaimBook(sessions)
    cara = await book.join(bill.id, "Cara")
    message = text("6593333333", "CLAIM K7Q2AB 2", "cara")
    await chat(sessions).by_code(message, "CLAIM K7Q2AB 2", FakeTransport())
    async with sessions() as session:
        people = (await session.execute(
            select(Participant).where(Participant.guest_name.ilike("cara"))
        )).scalars().all()
    assert [person.id for person in people] == [cara]
    assert people[0].user_id is not None


# the payer assigns everyone in words, sees the shares, and only Looks right saves
async def test_payer_assigns_in_words(sessions):
    await seed(sessions)
    reading = ClaimMap(people=[
        PersonClaims(name="Anna", items=[ClaimPart(item=3)]),
        PersonClaims(name="Ben", items=[ClaimPart(item=3), ClaimPart(item=2)]),
        PersonClaims(items=[ClaimPart(item=1)])
    ])
    claims = chat(sessions, reading)
    transport = FakeTransport()
    words = "Anna and Ben shared the calamari, Ben had a pasta, I had the other"
    await claims.free_text(text(PAYER, words), words, transport)
    _, body, buttons = transport.sent_buttons[-1]
    # 7000 over 5800 of food, anna 900, ben 2900 and tommy 2000
    assert "- Anna: Calamari · *SGD 10.86*" in body
    assert "- Ben: Calamari, Pasta · *SGD 35.00*" in body
    assert "- You: Pasta · *SGD 24.14*" in body
    assert buttons == [("claim:ok", "Looks right"), ("claim:no", "Change")]
    async with sessions() as session:
        assert (await session.execute(select(ItemClaim))).first() is None
    await claims.on_button(tap(PAYER, "claim:ok"), "claim:ok", transport)
    assert transport.sent_text[-1][1].startswith("*Saved*\nYour share so far: *SGD 24.14*")
    async with sessions() as session:
        assert len((await session.execute(select(ItemClaim))).all()) == 4


# words that match nothing, and Change, save nothing
async def test_no_match_and_change(sessions):
    await seed(sessions)
    transport = FakeTransport()
    await chat(sessions, ClaimMap()).free_text(text(PAYER, "hmm"), "hmm", transport)
    assert transport.sent_text[-1][1] == claim_module.NO_MATCH.format("K7Q2AB")
    reading = ClaimMap(people=[PersonClaims(items=[ClaimPart(item=1, portion=0.5)])])
    claims = chat(sessions, reading)
    await claims.free_text(text(PAYER, "half a pasta"), "half a pasta", transport)
    assert "- You: half of Pasta" in transport.sent_buttons[-1][1]
    await claims.on_button(tap(PAYER, "claim:no"), "claim:no", transport)
    assert transport.sent_text[-1][1] == claim_module.CHANGE
    async with sessions() as session:
        assert (await session.execute(select(ItemClaim))).first() is None


# a caption with no claims in it stays silent on the new bill
async def test_quiet_caption(sessions):
    await seed(sessions)
    transport = FakeTransport()
    await chat(sessions, ClaimMap()).free_text(
        text(PAYER, "split with the team"), "split with the team", transport, "K7Q2AB", quiet=True
    )
    assert transport.sent_text == [] and transport.sent_buttons == []


# the list also carries the person's own link to the bill page
async def test_code_sends_bill_link(sessions):
    await seed(sessions)
    links = FakeLinks()
    transport = FakeTransport()
    await chat(sessions, links=links).by_code(
        text(ANNA, "CLAIM K7Q2AB", "Anna"), "CLAIM K7Q2AB", transport
    )
    assert links.bills == ["K7Q2AB"]
    page = "Or tick them on the web:\nhttps://sl.test/b/K7Q2AB/token"
    assert transport.sent_buttons[-1][1].endswith(page)


# after the list, bare numbers claim on that bill without the code
async def test_bare_numbers(sessions):
    await seed(sessions)
    claims = chat(sessions)
    transport = FakeTransport()
    assert not claims.has_bill(ANNA)
    await claims.by_code(text(ANNA, "CLAIM K7Q2AB", "Anna"), "CLAIM K7Q2AB", transport)
    assert claims.has_bill(ANNA)
    await claims.numbers(text(ANNA, "1, 3", "Anna"), "1, 3", transport)
    async with sessions() as session:
        assert len((await session.execute(select(ItemClaim))).all()) == 2
