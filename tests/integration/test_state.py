from datetime import timedelta

from sqlalchemy import func, select

from splitledger.contracts.messages import Inbound
from splitledger.db.models import BudgetPlan, ChatState, LedgerEntry
from splitledger.db.state import DbState
from splitledger.handlers.log import ExpenseLogger
from splitledger.handlers.plan import PlanManager
from splitledger.llm.schemas import GLOBAL, ReceiptDraft
from splitledger.plans.quota import QuotaBook
from tests.fakes import FakeLLM, FakeTransport
from tests.integration.test_claim_chat import ANNA, chat, text
from tests.integration.test_claims import seed
from tests.integration.test_plans import ME, msg

HOUR = timedelta(hours=1)


# values come back until popped, a newer put replaces the older one
async def test_put_get_pop(sessions):
    state = DbState(sessions)
    assert await state.get("k", "a") is None
    await state.put("k", "a", {"n": 1}, HOUR)
    await state.put("k", "a", {"n": 2}, HOUR)
    assert await state.get("k", "a") == {"n": 2}
    assert await state.get("other", "a") is None
    assert await state.pop("k", "a") == {"n": 2}
    assert await state.pop("k", "a") is None


# expired values read as missing and are cleared by the next write
async def test_expiry(sessions):
    state = DbState(sessions)
    await state.put("k", "old", {"n": 1}, -HOUR)
    assert await state.get("k", "old") is None
    assert await state.pop("k", "old") is None
    await state.put("k", "stale", {"n": 1}, -HOUR)
    await state.put("k", "new", {"n": 2}, HOUR)
    async with sessions() as session:
        count = select(func.count()).select_from(ChatState)
        assert (await session.execute(count)).scalar_one() == 1


# a new claim chat, as after a restart, still knows which bill a friend opened
async def test_claim_bill_survives_restart(sessions):
    await seed(sessions)
    transport = FakeTransport()
    await chat(sessions).by_code(text(ANNA, "CLAIM K7Q2AB", "Anna"), "CLAIM K7Q2AB", transport)
    assert await chat(sessions).has_bill(ANNA)


# a plan set up carries on with a new plan manager, as after a restart
async def test_plan_draft_survives_restart(sessions):
    transport = FakeTransport()
    first = PlanManager(sessions, QuotaBook(sessions), GLOBAL, FakeLLM())
    for button in ["plan:setup", "plan:fw:503020"]:
        await first.on_button(msg("button", button), button, transport)
    again = PlanManager(sessions, QuotaBook(sessions), GLOBAL, FakeLLM())
    assert await again.waiting_for_text(ME)
    await again.on_text(msg("text", "4500"), transport)
    for button in ["plan:monthly", "plan:save"]:
        await again.on_button(msg("button", button), button, transport)
    async with sessions() as session:
        plan = (await session.execute(select(BudgetPlan))).scalar_one()
    assert (plan.framework, plan.period) == ("503020", "monthly")
    assert not await again.waiting_for_text(ME)


# yes save it works on a receipt read before a restart
async def test_receipt_survives_restart(sessions):
    receipt = ReceiptDraft(total_cents=1500, subtotal_cents=900, category="Dining")
    transport = FakeTransport()
    transport.media["photo-1"] = b"jpeg-bytes"
    llm = FakeLLM({"parse_receipt": receipt})
    photo = Inbound("m1", "14155550100", "image", media_ref="photo-1")
    await ExpenseLogger(llm, sessions, GLOBAL).log_receipt(photo, transport)
    key = transport.sent_buttons[0][2][0][0].removeprefix("receipt:")
    await ExpenseLogger(FakeLLM(), sessions, GLOBAL).confirm_receipt(photo, key, transport)
    async with sessions() as session:
        amounts = (await session.execute(select(LedgerEntry.amount_cents))).scalars().all()
    assert amounts == [1500]
