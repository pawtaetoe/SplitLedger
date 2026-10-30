from sqlalchemy import select

from splitledger.contracts.messages import Inbound
from splitledger.db.models import BudgetPlan, User
from splitledger.handlers.log import ExpenseLogger, quick_parse
from splitledger.handlers.plan import PlanManager
from splitledger.llm.schemas import GLOBAL, BucketDraft, CustomPlan
from splitledger.plans.quota import QuotaBook
from tests.fakes import FakeLLM, FakeTransport

ME = "14155550100"


# one message from me
def msg(kind: str, value: str) -> Inbound:
    if kind == "button":
        return Inbound("m1", ME, "button", button_id=value)
    return Inbound("m1", ME, "text", text=value)


# set up 50/30/20 on 4500 a month, checked weekly
async def set_up(plans: PlanManager, transport: FakeTransport) -> None:
    await plans.on_button(msg("button", "plan:setup"), "plan:setup", transport)
    await plans.on_button(msg("button", "plan:fw:503020"), "plan:fw:503020", transport)
    assert plans.waiting_for_text(ME)
    await plans.on_text(msg("text", "4500"), transport)
    await plans.on_button(msg("button", "plan:weekly"), "plan:weekly", transport)
    await plans.on_button(msg("button", "plan:save"), "plan:save", transport)


# log one expense the rules can read and return the reply text
async def log(logger: ExpenseLogger, transport: FakeTransport, body: str) -> str:
    draft = quick_parse(body, GLOBAL)
    assert draft is not None
    await logger.log(msg("text", body), transport, draft)
    return transport.sent_buttons[-1][1]


# the welcome goes out on the first message only
async def test_welcome_once(sessions):
    plans = PlanManager(sessions, QuotaBook(sessions), GLOBAL, FakeLLM())
    transport = FakeTransport()
    await plans.welcome_if_new(msg("text", "hi"), transport)
    await plans.welcome_if_new(msg("text", "hi again"), transport)
    assert len(transport.sent_buttons) == 1
    assert transport.sent_buttons[0][2][0] == ("menu:split", "Split a bill")


# set up saves the plan and income and shows weekly shares before saving
async def test_set_up_saves_plan(sessions):
    plans = PlanManager(sessions, QuotaBook(sessions), GLOBAL, FakeLLM())
    transport = FakeTransport()
    await set_up(plans, transport)
    confirm = transport.sent_buttons[-1][1]
    assert "- Needs: *USD 519.23*\n- Wants: *USD 311.53*\n- Savings: *USD 207.69*" in confirm
    assert "All Your Worth" in confirm
    async with sessions() as session:
        plan = (await session.execute(select(BudgetPlan))).scalar_one()
        user = (await session.execute(select(User))).scalar_one()
    assert (plan.framework, plan.period, user.take_home_cents) == ("503020", "weekly", 450000)
    assert transport.sent_text[-1][1].startswith("✅ *Plan saved*")


# each log shows what is left and alerts once at 80 and once at 100
async def test_quota_lines_and_alerts(sessions):
    book = QuotaBook(sessions)
    plans = PlanManager(sessions, book, GLOBAL, FakeLLM())
    transport = FakeTransport()
    await set_up(plans, transport)
    logger = ExpenseLogger(FakeLLM(), sessions, GLOBAL, book)
    first = await log(logger, transport, "lunch 50")
    assert first.endswith("*USD 261.53* left in Wants this week")
    second = await log(logger, transport, "dinner 200")
    assert "*Heads up:* 80% of Wants used" in second
    assert "Heads up" not in await log(logger, transport, "coffee 10")
    last = await log(logger, transport, "movie 100")
    assert "*USD 48.47* over in Wants this week" in last
    assert "you've used all of Wants this week." in last
    assert (await log(logger, transport, "rent 5")).endswith("*USD 514.23* left in Needs this week")


# without a plan the reply stays one line
async def test_no_plan_no_line(sessions):
    logger = ExpenseLogger(FakeLLM(), sessions, GLOBAL, QuotaBook(sessions))
    transport = FakeTransport()
    assert await log(logger, transport, "lunch 50") == "✅ *Logged USD 50.00*\nDining · lunch"


# undone spending no longer counts against the plan
async def test_undo_frees_quota(sessions):
    book = QuotaBook(sessions)
    transport = FakeTransport()
    await set_up(PlanManager(sessions, book, GLOBAL, FakeLLM()), transport)
    logger = ExpenseLogger(FakeLLM(), sessions, GLOBAL, book)
    await log(logger, transport, "lunch 50")
    await logger.undo_last(msg("text", "/undo"), transport)
    entry_id = transport.sent_buttons[-1][2][0][0].removeprefix("undo:")
    await logger.undo(msg("text", "yes"), entry_id, transport)
    after = await log(logger, transport, "coffee 1")
    assert after.endswith("*USD 310.53* left in Wants this week")


# the assistant tool gets plain numbers and removing the plan clears it
async def test_status_and_remove(sessions):
    book = QuotaBook(sessions)
    plans = PlanManager(sessions, book, GLOBAL, FakeLLM())
    transport = FakeTransport()
    await set_up(plans, transport)
    async with sessions() as session:
        user_id = (await session.execute(select(User.id))).scalar_one()
    status = await book.status(user_id)
    assert status["plan"] == "503020" and status["period"] == "week"
    savings = {"name": "Savings", "target": "USD 207.69", "spent": None, "left": None}
    assert status["buckets"][2] == savings
    await plans.on_button(msg("button", "plan:remove:yes"), "plan:remove:yes", transport)
    assert (await book.status(user_id))["plan"] is None


# a plan typed in words: the model drafts it, python checks and converts the limit
async def test_own_plan_with_a_limit(sessions):
    book = QuotaBook(sessions)
    drafted = CustomPlan(
        buckets=[
            BucketDraft(name="Food", cap_cents=40000, cap_per="month",
                        categories=["Dining", "Groceries"]),
            BucketDraft(name="Savings", pct=30)
        ],
        period="monthly"
    )
    plans = PlanManager(sessions, book, GLOBAL, FakeLLM({"custom_plan": drafted}))
    transport = FakeTransport()
    await plans.on_button(msg("button", "plan:setup"), "plan:setup", transport)
    await plans.on_button(msg("button", "plan:more"), "plan:more", transport)
    await plans.on_button(msg("button", "plan:fw:custom"), "plan:fw:custom", transport)
    assert plans.waiting_for_text(ME)
    await plans.on_text(msg("text", "save 30%, food under 400 a month"), transport)
    await plans.on_text(msg("text", "4500"), transport)
    confirm = transport.sent_buttons[-1][1]
    assert "- Food: *USD 400.00*\n- Savings: *USD 1,350.00*" in confirm
    untracked = "transport, shopping, bills, entertainment, health, travel, other"
    assert f"_Not tracked: {untracked}._" in confirm
    assert "_Your own plan, drafted from your words_" in confirm
    await plans.on_button(msg("button", "plan:save"), "plan:save", transport)
    logger = ExpenseLogger(FakeLLM(), sessions, GLOBAL, book)
    lunch = await log(logger, transport, "lunch 50")
    assert lunch.endswith("*USD 350.00* left in Food this month")
    assert "left in" not in await log(logger, transport, "uber 20")


# two plans that do not add up lead back to the ready-made ones
async def test_own_plan_refused_twice(sessions):
    drafted = CustomPlan(
        buckets=[BucketDraft(name="Savings", pct=70), BucketDraft(name="Fun", pct=50)]
    )
    plans = PlanManager(sessions, QuotaBook(sessions), GLOBAL, FakeLLM({"custom_plan": drafted}))
    transport = FakeTransport()
    await plans.on_button(msg("button", "plan:setup"), "plan:setup", transport)
    await plans.on_button(msg("button", "plan:fw:custom"), "plan:fw:custom", transport)
    await plans.on_text(msg("text", "save 70% and 50% fun"), transport)
    assert "The shares add up to 120%" in transport.sent_buttons[-1][1]
    await plans.on_text(msg("text", "save 70% and 50% fun"), transport)
    assert transport.sent_text[-1][1].startswith("*Let's try a ready-made plan*")
    assert transport.sent_buttons[-1][1].startswith("*Pick a plan*")


# when the model is down the user can retry or pick a ready-made plan
async def test_own_plan_model_down(sessions):
    plans = PlanManager(sessions, QuotaBook(sessions), GLOBAL, FakeLLM())
    transport = FakeTransport()
    await plans.on_button(msg("button", "plan:setup"), "plan:setup", transport)
    await plans.on_button(msg("button", "plan:fw:custom"), "plan:fw:custom", transport)
    await plans.on_text(msg("text", "save half"), transport)
    assert transport.sent_buttons[-1][1].startswith("*I couldn't read that just now*")
    assert plans.waiting_for_text(ME)
