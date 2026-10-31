from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select

from splitledger.contracts.messages import Inbound
from splitledger.db.models import LedgerEntry, PlannedSpend, User
from splitledger.handlers import bills as bills_module
from splitledger.handlers.bills import UpcomingBills
from splitledger.handlers.log import ExpenseLogger
from splitledger.llm.schemas import GLOBAL
from tests.fakes import FakeLLM, FakeTransport

ME = "6581234567"
# 9 30 am in singapore on the 30th, rent is due the next day
NOW = datetime(2026, 10, 30, 1, 30, tzinfo=UTC)
DUE = date(2026, 10, 31)


# a user with one monthly bill
async def seed(sessions, remind: bool = False) -> PlannedSpend:
    me = User(wa_user_id=ME, currency="SGD", timezone="Asia/Singapore")
    async with sessions() as session:
        session.add(me)
        await session.flush()
        rent = PlannedSpend(
            user_id=me.id, label="rent", amount_cents=180000, currency="SGD", category="Bills",
            every="month", due_on=date(2026, 8, 31), remind=remind
        )
        session.add(rent)
        await session.commit()
    return rent


# the bills handler with a real logger
def handler(sessions) -> UpcomingBills:
    return UpcomingBills(sessions, ExpenseLogger(FakeLLM(), sessions, GLOBAL), 9)


# one button tap
def tap(button_id: str) -> Inbound:
    return Inbound("m1", ME, "button", button_id=button_id)


# reminders go out once per due day, only after the remind hour and only when switched on
async def test_send_due(sessions):
    rent = await seed(sessions)
    bills = handler(sessions)
    transport = FakeTransport()
    assert await bills.send_due(transport, NOW) == 0
    await bills.on_button(tap(f"bills:remind:{rent.id}"), f"bills:remind:{rent.id}", transport)
    assert transport.sent_text[-1][1] == bills_module.REMIND_ON
    assert await bills.send_due(transport, NOW - timedelta(hours=2)) == 0
    assert await bills.send_due(transport, NOW) == 1
    assert await bills.send_due(transport, NOW + timedelta(minutes=10)) == 0
    _, text, buttons = transport.sent_buttons[-1]
    assert text == "*Reminder: rent SGD 1,800.00*\nDue tomorrow, Sat 31 Oct."
    assert buttons[0] == (f"bills:paid:{rent.id}:{DUE}", "Paid it")


# paid saves the expense once and drops that day from what is still due
async def test_paid_logs_once(sessions):
    rent = await seed(sessions, remind=True)
    bills = handler(sessions)
    transport = FakeTransport()
    paid = f"bills:paid:{rent.id}:{DUE}"
    await bills.on_button(tap(paid), paid, transport)
    await bills.on_button(tap(paid), paid, transport)
    assert transport.sent_text[-1][1] == bills_module.ALREADY
    async with sessions() as session:
        notes = (await session.execute(select(LedgerEntry.note, LedgerEntry.amount_cents))).all()
        stored = await session.get(PlannedSpend, rent.id)
    assert notes == [("rent", 180000)]
    assert stored is not None and stored.paid_through == DUE
    assert await bills.send_due(transport, NOW) == 0


# the list shows each bill once with its next day, undo removes it, other users can't
async def test_show_and_undo(sessions):
    rent = await seed(sessions)
    bills = handler(sessions)
    transport = FakeTransport()
    await bills.show(tap("menu:bills"), transport)
    _, text, buttons = transport.sent_buttons[-1]
    assert text.startswith("*Upcoming bills*\n- rent *SGD 1,800.00*")
    assert buttons == [(f"bills:pick:{rent.id}", "rent")]
    stranger = Inbound("m2", "6599999999", "button")
    undo = f"bills:undo:{rent.id}"
    await bills.on_button(stranger, undo, transport)
    assert transport.sent_text[-1][1] == bills_module.ALREADY
    await bills.on_button(tap(undo), undo, transport)
    assert transport.sent_text[-1][1] == bills_module.UNDONE.format("rent")
    await bills.show(tap("menu:bills"), transport)
    assert transport.sent_text[-1][1] == bills_module.NONE_YET


# picking a bill offers remove and a reminder switch that flips, other users can't pick it
async def test_pick_switches_reminder(sessions):
    rent = await seed(sessions, remind=True)
    bills = handler(sessions)
    transport = FakeTransport()
    pick = f"bills:pick:{rent.id}"
    await bills.on_button(Inbound("m2", "6599999999", "button"), pick, transport)
    assert transport.sent_text[-1][1] == bills_module.GONE
    await bills.on_button(tap(pick), pick, transport)
    _, text, buttons = transport.sent_buttons[-1]
    assert text == "*rent SGD 1,800.00*\nEvery month · Reminder on"
    quiet = f"bills:quiet:{rent.id}"
    assert buttons == [(f"bills:undo:{rent.id}", "Remove"), (quiet, "Reminders off")]
    await bills.on_button(tap(quiet), quiet, transport)
    assert transport.sent_text[-1][1] == bills_module.REMIND_OFF
    assert await bills.send_due(transport, NOW) == 0
    await bills.on_button(tap(pick), pick, transport)
    assert transport.sent_buttons[-1][2][1] == (f"bills:remind:{rent.id}", "Remind me")
