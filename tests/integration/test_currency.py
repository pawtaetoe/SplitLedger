from sqlalchemy import select

from splitledger.contracts.messages import Inbound
from splitledger.db.models import LedgerEntry, User
from splitledger.handlers.currency import UNKNOWN, CurrencyChoice
from splitledger.handlers.log import ExpenseLogger
from splitledger.llm.schemas import ExpenseDraft
from splitledger.llm.schemas_sg import SG
from tests.fakes import FakeLLM, FakeTransport

MALAYSIA = "60123456789"


# one text from the malaysian user
def text(body: str) -> Inbound:
    return Inbound(f"m-{body}", MALAYSIA, "text", text=body)


# the currency of the user's row
async def currency_of(sessions) -> str:
    async with sessions() as session:
        user = (await session.execute(select(User).where(User.wa_user_id == MALAYSIA))).scalar_one()
    return user.currency


# a new user gets their phone's currency even on a singapore setup, and logs in it
async def test_phone_sets_currency(sessions):
    logger = ExpenseLogger(FakeLLM(), sessions, SG)
    transport = FakeTransport()
    await logger.log(text("teh 2"), transport, ExpenseDraft(amount_cents=200, category="Dining"))
    assert await currency_of(sessions) == "MYR"
    assert transport.sent_buttons[-1][1].startswith("✅ *Logged MYR 2.00*")


# the list shows the current one first, a tap or typing changes it, nonsense is refused
async def test_change_currency(sessions):
    await ExpenseLogger(FakeLLM(), sessions, SG).log(
        text("teh 2"), FakeTransport(), ExpenseDraft(amount_cents=200, category="Dining")
    )
    choice = CurrencyChoice(sessions)
    transport = FakeTransport()
    await choice.ask(text("menu"), transport)
    _, ask, buttons = transport.sent_buttons[-1]
    assert ask.startswith("*Your main currency is MYR*")
    assert buttons[0] == ("cur:MYR", "MYR — Malaysian ringgit") and len(buttons) <= 10
    await choice.on_button(text("tap"), "cur:SGD", transport)
    assert await currency_of(sessions) == "SGD"
    assert await choice.typed(text("currency jpy"), "currency jpy", transport)
    assert await currency_of(sessions) == "JPY"
    assert await choice.typed(text("currency xyz"), "currency xyz", transport)
    assert transport.sent_text[-1][1] == UNKNOWN.format("XYZ")
    assert await currency_of(sessions) == "JPY"
    assert not await choice.typed(text("coffee 4"), "coffee 4", transport)
    async with sessions() as session:
        logged = (await session.execute(select(LedgerEntry.currency))).scalars().all()
    assert logged == ["MYR"]
