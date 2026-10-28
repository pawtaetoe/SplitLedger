import asyncio

import pytest

import splitledger.handlers.router as router_module
from splitledger.contracts.messages import Inbound
from splitledger.db.models import Bill, LedgerSource
from splitledger.handlers.router import HELP, UNCLEAR, MessageReader, Router
from splitledger.llm.schemas import ExpenseDraft, MessageResult
from splitledger.llm.schemas_sg import SG
from tests import fakes
from tests.fakes import FakeAssistant, FakeBills, FakeLLM, FakeLogger, FakePlans, FakeTransport
from tests.fakes.router import fake_router

USER = "6581234567"


# one text message
def text(body: str) -> Inbound:
    return Inbound("m1", USER, "text", text=body)


# one button tap
def button(button_id: str) -> Inbound:
    return Inbound("m1", USER, "button", button_id=button_id)


# one photo with an optional caption
def image(caption: str | None) -> Inbound:
    return Inbound("m1", USER, "image", text=caption, media_ref="photo-1")


# one voice note with an optional media reference
def audio(media_ref: str | None) -> Inbound:
    return Inbound("m1", USER, "audio", media_ref=media_ref)


# a model reply for one intent
def said(intent: str, cents: int | None = None) -> MessageResult:
    expense = ExpenseDraft(amount_cents=cents, category="Dining") if cents else None
    return MessageResult(intent=intent, expense=expense)


# a router with fakes for every handler not given
def build(llm: FakeLLM, logger=None, assistant=None, **given) -> Router:
    return fake_router(llm, SG, logger=logger, assistant=assistant, **given)


# name the handler a message reached from the fakes or the reply text
def landed(
    logger: FakeLogger, transport: FakeTransport, assistant: FakeAssistant,
    group: fakes.FakeGroup, claims: fakes.FakeClaims
) -> str:
    if assistant.asked:
        return "ask"
    if claims.calls:
        return "claim"
    replies = [reply for _, reply in transport.sent_text]
    replies += [reply for _, reply, _ in transport.sent_buttons]
    if group.calls or router_module.SPLIT_HINT in replies:
        return "split"
    if HELP in replies:
        return "help"
    if UNCLEAR in replies:
        return "unclear"
    calls = [call for call in logger.calls if call != "transcribe"]
    return calls[-1] if calls else "echo"


# route one message with a fake logger and an optional canned model reply
async def route(
    inbound: Inbound, reply: MessageResult | None
) -> tuple[str, FakeLogger, FakeLLM]:
    llm = FakeLLM({"read_message": reply} if reply else {})
    logger = FakeLogger()
    transport = FakeTransport()
    assistant = FakeAssistant()
    group = fakes.FakeGroup()
    claims = fakes.FakeClaims()
    await build(llm, logger, assistant, group=group, claims=claims).handle(inbound, transport)
    return landed(logger, transport, assistant, group, claims), logger, llm


# bot formats are handled by rule and never reach the model
PROTOCOL_ROWS = [
    (text("/help"), "help"),
    (text("/HELP"), "help"),
    (text("/undo"), "undo_last"),
    (text("/budget 600"), "help"),
    (text("CLAIM K7Q2"), "claim"),
    (text("claim k7q2 1,3"), "claim"),
    (button("undo:abc"), "undo"),
    (button("receipt:k1"), "confirm_receipt"),
    (text("menu"), "help"),
    (text("Help"), "help"),
    (button("save"), "echo"),
    (image(None), "log_receipt"),
    (audio(None), "echo")
]


@pytest.mark.parametrize(("inbound", "expected"), PROTOCOL_ROWS)
async def test_protocol_rows(inbound, expected):
    where, _, llm = await route(inbound, None)
    assert where == expected
    assert llm.structured_calls == []


# free text goes to the model once and lands where the model says
MODEL_ROWS = [
    (text("lunch with the team, about 25 bucks"), said("log", 2500), "log"),
    (text("makan 12 just now lah"), said("log", 1200), "log"),
    (text("I split my phone screen, repair 120"), said("log", 12000), "log"),
    (text("can I afford a 300 hotel?"), said("ask"), "ask"),
    (text("mom's birthday present 150 on 3 dec"), said("upcoming"), "ask"),
    (text("let's share dinner four ways"), said("split"), "split"),
    (text("the laksa was mine"), said("claim"), "claim"),
    (text("hello"), said("chat"), "help"),
    (text("asdfgh"), said("unclear"), "unclear"),
    (image("split this with the team"), said("split"), "split"),
    (text("split the overall charge evenly among 4 pax"), MessageResult(intent="split", ways=4),
     "split"),
    (image("lunch"), said("log", 1500), "log_receipt")
]


@pytest.mark.parametrize(("inbound", "reply", "expected"), MODEL_ROWS)
async def test_model_rows(inbound, reply, expected):
    where, _, llm = await route(inbound, reply)
    assert where == expected
    assert len(llm.structured_calls) == 1


# a log with no amount from the model still reaches the logger to ask for one
async def test_log_without_expense_asks_logger():
    _, logger, _ = await route(text("bought stuff"), said("log"))
    assert logger.drafts[0][0].amount_cents is None


# a voice note is transcribed then read like text and saved as voice
async def test_voice_goes_through_reader():
    llm = FakeLLM({"read_message": said("log", 4500)})
    logger = FakeLogger()
    logger.heard = "spent 45 on petrol"
    router = build(llm, logger)
    await router.handle(audio("note-1"), FakeTransport())
    assert logger.calls == ["transcribe", "log"]
    assert logger.drafts[0][1] == LedgerSource.VOICE


# a spoken question is answered as a question not logged
async def test_spoken_question_is_not_logged():
    llm = FakeLLM({"read_message": said("ask")})
    logger = FakeLogger()
    logger.heard = "how much did I spend this week"
    assistant = FakeAssistant()
    router = build(llm, logger, assistant)
    await router.handle(audio("note-1"), FakeTransport())
    assert assistant.asked == ["how much did I spend this week"]


# when the model fails the keyword rules still route the basics
FALLBACK_ROWS = [
    (text("coffee 6.50"), "log"),
    (text("kopi 1.80"), "log"),
    (text("split this"), "split"),
    (text("how much did I spend?"), "ask"),
    (text("rent 1200 every month"), "ask"),
    (text("hello"), "unclear")
]


@pytest.mark.parametrize(("inbound", "expected"), FALLBACK_ROWS)
async def test_fallback_rows(inbound, expected):
    where, _, _ = await route(inbound, None)
    assert where == expected


# a slow model is cut off and the rules answer instead
async def test_slow_model_falls_back(monkeypatch):
    monkeypatch.setattr(router_module, "MODEL_TIMEOUT", 0.01)

    class SlowLLM(FakeLLM):
        async def structured(self, prompt, schema, images=()):
            await asyncio.sleep(1)
            return await super().structured(prompt, schema, images)

    result = await MessageReader(SlowLLM(), SG).read("coffee 6.50")
    assert (result.intent, result.expense.amount_cents) == ("log", 650)


# plan buttons, the plan command, income replies and plan talk reach the plan manager
async def test_plan_routes():
    plans = FakePlans()
    llm = FakeLLM({"read_message": said("plan")})
    router = build(llm, plans=plans)
    transport = FakeTransport()
    await router.handle(button("plan:fw:503020"), transport)
    await router.handle(text("/plan"), transport)
    await router.handle(text("set up a budget for me"), transport)
    plans.waiting = True
    await router.handle(text("4500"), transport)
    assert plans.calls == [
        "welcome_if_new", "button plan:fw:503020",
        "welcome_if_new", "show",
        "welcome_if_new", "show",
        "welcome_if_new", "on_text"
    ]


# bills buttons and the bills menu choice reach the bills handler
async def test_bills_routes():
    bills = FakeBills()
    router = build(FakeLLM(), bills=bills)
    await router.handle(button("bills:remind:abc"), FakeTransport())
    await router.handle(button("menu:bills"), FakeTransport())
    assert bills.calls == ["button bills:remind:abc", "show"]


# the menu offers every action, and each choice gives examples or opens its handler
async def test_menu_choices():
    logger = FakeLogger()
    plans = FakePlans()
    router = build(FakeLLM(), logger, plans=plans)
    transport = FakeTransport()
    await router.handle(text("/help"), transport)
    assert transport.sent_buttons[-1] == (USER, HELP, router_module.MENU)
    await router.handle(button("menu:log"), transport)
    assert transport.sent_text[-1][1] == router_module.HINTS["menu:log"]
    await router.handle(button("menu:plan"), transport)
    await router.handle(button("menu:undo"), transport)
    assert "show" in plans.calls and logger.calls[-1] == "undo_last"
    await router.handle(button("menu:open"), transport)
    assert transport.sent_buttons[-1][2] == router_module.MENU


# a head count turns a split into an even split, by photo caption or by message after it
async def test_even_split_routes():
    group = fakes.FakeGroup()
    llm = FakeLLM({"read_message": MessageResult(intent="split", ways=4)})
    router = build(llm, group=group)
    await router.handle(image("split 4 ways"), FakeTransport())
    await router.handle(text("split it evenly among 4 pax"), FakeTransport())
    assert group.calls == ["split 4", "even 4"]


# the rules find a head count when the model is down
def test_rules_read_ways():
    reader = MessageReader(FakeLLM(), SG)
    assert reader.by_rules("split this 3 ways").ways == 3
    assert reader.by_rules("split among 5 pax").ways == 5
    assert reader.by_rules("split this").ways is None
    assert reader.by_rules("split this 7 ways by dish").ways is None
    assert reader.by_rules("split this 7 ways by dish").split_mode == "items"
    assert reader.by_rules("split this").split_mode == "items"
    assert reader.by_rules("split 4 ways").split_mode == "even"


# claim codes, claim buttons and claims in words reach the claim chat
async def test_claim_routes():
    claims = fakes.FakeClaims()
    llm = FakeLLM({"read_message": said("claim")})
    router = build(llm, claims=claims)
    await router.handle(text("CLAIM K7Q2AB 1,3"), FakeTransport())
    await router.handle(button("claim:ok"), FakeTransport())
    await router.handle(text("Anna and Ben shared the calamari"), FakeTransport())
    assert claims.calls == [
        "code CLAIM K7Q2AB 1,3", "button claim:ok", "words Anna and Ben shared the calamari"
    ]


# close buttons and closing in words reach the group bill handler
async def test_close_routes():
    group = fakes.FakeGroup()
    router = build(FakeLLM(), group=group)
    await router.handle(button("bill:close:K7Q2AB"), FakeTransport())
    await router.handle(text("close the bill"), FakeTransport())
    assert group.calls == ["button bill:close:K7Q2AB", "close"]


# i've paid and not received reach the group bill handler too
async def test_pay_routes():
    group = fakes.FakeGroup()
    router = build(FakeLLM(), group=group)
    await router.handle(button("pay:done:x:SGD"), FakeTransport())
    assert group.calls == ["button pay:done:x:SGD"]


# a caption saying who had what is read as claims on the bill it just made
async def test_caption_claims_after_split():
    group = fakes.FakeGroup()
    group.bill = Bill(code="K7Q2AB")
    claims = fakes.FakeClaims()
    llm = FakeLLM({"read_message": MessageResult(intent="split")})
    router = build(llm, group=group, claims=claims)
    caption = "split 7 ways by dish, I had the rosti and shared the beer"
    await router.handle(image(caption), FakeTransport())
    await router.handle(image("split"), FakeTransport())
    assert group.calls == ["split None", "split None"]
    assert claims.calls == [f"words {caption} on K7Q2AB"]


# an unsure split asks the bill handler to check how before sharing anything
async def test_unsure_split_asks():
    group = fakes.FakeGroup()
    llm = FakeLLM({"read_message": MessageResult(intent="split", split_mode="unsure")})
    await build(llm, group=group).handle(image("split this"), FakeTransport())
    assert group.calls == ["split None unsure"]


# the menu choice and the word dashboard send the sign in link without the model
async def test_dashboard_routes():
    dash = fakes.FakeDash()
    llm = FakeLLM()
    router = build(llm, dash=dash)
    await router.handle(button("menu:dash"), FakeTransport())
    await router.handle(text("my dashboard"), FakeTransport())
    await router.handle(button("plan:web"), FakeTransport())
    assert dash.pages == ["me", "me", "plan"] and llm.structured_calls == []


# a friend arriving with a claim code gets the bill, not the welcome
async def test_claims_skip_welcome():
    plans = FakePlans()
    router = build(FakeLLM(), plans=plans)
    await router.handle(text("CLAIM 7RUKTW"), FakeTransport())
    await router.handle(button("claim:pick:7RUKTW:3"), FakeTransport())
    assert "welcome_if_new" not in plans.calls
    await router.handle(text("/help"), FakeTransport())
    assert plans.calls == ["welcome_if_new"]


# bare numbers are claims only while a bill is open in the chat
async def test_bare_numbers_claim():
    claims = fakes.FakeClaims()
    llm = FakeLLM({"read_message": said("unclear")})
    router = build(llm, claims=claims)
    await router.handle(text("2, 6"), FakeTransport())
    assert claims.calls == []
    claims.open = True
    await router.handle(text("2, 6"), FakeTransport())
    await router.handle(text("1 and 3"), FakeTransport())
    assert claims.calls == ["numbers 2, 6", "numbers 1 and 3"]


# the button under a you owe message lists everything owed without the model
async def test_owe_all_button():
    assistant = FakeAssistant()
    llm = FakeLLM()
    await build(llm, assistant=assistant).handle(button("owe:all"), FakeTransport())
    assert assistant.asked == ["owed"] and llm.structured_calls == []
