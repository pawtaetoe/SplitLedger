from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from splitledger.contracts.llm import ToolRunResult
from splitledger.contracts.messages import Inbound
from splitledger.db.models import LedgerEntry, LedgerKind, User
from splitledger.handlers.ask import CHECKING, NOTHING_YET, UNAVAILABLE, SpendAssistant, SpendTools
from splitledger.plans.quota import QuotaBook
from tests.fakes import FakeLLM, FakeTransport

SG = ZoneInfo("Asia/Singapore")
ME = "6581234567"


# a user with spending on known days, plus someone else and a voided entry
async def seed(sessions) -> User:
    me = User(wa_user_id=ME, currency="SGD", timezone="Asia/Singapore")
    other = User(wa_user_id="6599999999", currency="SGD", timezone="Asia/Singapore")
    async with sessions() as session:
        session.add_all([me, other])
        await session.flush()
        # 11 30 pm on the 27th in singapore is still the 27th there
        late = datetime(2026, 10, 27, 15, 30, tzinfo=UTC)
        rows = [
            (me, 650, "Dining", "kopi", late, None),
            (me, 1420, "Transport", "grab", late - timedelta(days=1), None),
            (me, 4500, "Dining", "dinner", late - timedelta(days=2), None),
            (me, 9900, "Shopping", "shoes", late - timedelta(days=2), late),
            (other, 7000, "Dining", "steak", late, None)
        ]
        session.add_all(
            LedgerEntry(
                user_id=owner.id, kind=LedgerKind.EXPENSE, amount_cents=cents, currency="SGD",
                category=category, note=note, occurred_at=when, voided_at=voided
            )
            for owner, cents, category, note, when, voided in rows
        )
        await session.commit()
    return me


# the week of the 25th to the 27th
WEEK = {"start": "2026-10-25", "end": "2026-10-27"}


# sums match the seeded rows and skip voided and other users
async def test_sum_spend(sessions):
    me = await seed(sessions)
    tools = SpendTools(sessions, me.id, SG, QuotaBook(sessions))
    assert await tools.sum_spend(WEEK) == {"totals": [{"total": "SGD 65.70", "count": 3}]}
    dining = await tools.sum_spend({**WEEK, "category": "Dining"})
    assert dining == {"totals": [{"total": "SGD 51.50", "count": 2}]}


# a late night expense counts on its local day
async def test_local_day_boundary(sessions):
    me = await seed(sessions)
    tools = SpendTools(sessions, me.id, SG, QuotaBook(sessions))
    day = await tools.list_expenses({"start": "2026-10-27", "end": "2026-10-27"})
    assert [row["what"] for row in day["expenses"]] == ["kopi"]
    next_day = await tools.list_expenses({"start": "2026-10-28", "end": "2026-10-28"})
    assert next_day["expenses"] == []


# biggest first and capped at n
async def test_top_expenses(sessions):
    me = await seed(sessions)
    top = await SpendTools(sessions, me.id, SG, QuotaBook(sessions)).top_expenses({**WEEK, "n": 2})
    assert [row["amount"] for row in top["expenses"]] == ["SGD 45.00", "SGD 14.20"]


# the assistant says it is checking then sends the model answer
async def test_assistant_replies(sessions):
    await seed(sessions)
    llm = FakeLLM()
    llm.tool_result = ToolRunResult(text="You spent SGD 65.70 this week.")
    transport = FakeTransport()
    question = Inbound("m1", ME, "text", text="this week?")
    await SpendAssistant(llm, sessions, QuotaBook(sessions)).answer(question, transport)
    replies = [reply for _, reply in transport.sent_text]
    assert replies == [CHECKING, "You spent SGD 65.70 this week."]


# a model failure gives a plain retry message
async def test_assistant_failure(sessions):
    await seed(sessions)

    class BrokenLLM(FakeLLM):
        async def run_tools(self, system, user, tools):
            raise RuntimeError("tool loop did not finish")

    transport = FakeTransport()
    question = Inbound("m1", ME, "text", text="?")
    await SpendAssistant(BrokenLLM(), sessions, QuotaBook(sessions)).answer(question, transport)
    assert transport.sent_text[-1] == (ME, UNAVAILABLE)


# someone who never logged anything gets a hint and no model call
async def test_assistant_new_user(sessions):
    transport = FakeTransport()
    assistant = SpendAssistant(FakeLLM(), sessions, QuotaBook(sessions))
    await assistant.answer(Inbound("m1", ME, "text", text="?"), transport)
    assert transport.sent_text == [(ME, NOTHING_YET)]
