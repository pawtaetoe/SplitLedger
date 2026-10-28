from sqlalchemy import select

from splitledger.contracts.messages import Inbound
from splitledger.db.models import LedgerEntry, LedgerKind, User
from splitledger.handlers.settle import NOT_YOURS, OWE_ALL, SettleUp, paid_button
from splitledger.pay.rails import PLAIN, PayRails
from splitledger.pay.rails_sg import PayNow
from splitledger.plans.quota import QuotaBook
from splitledger.split.overview import Overview
from tests.fakes import FakeTransport

TOMMY, ANNA = "6581234567", "6591111111"


# tommy paid 10 for anna, anna paid 4 for tommy on another bill
async def debts(sessions) -> tuple[User, User]:
    tommy = User(wa_user_id=TOMMY, display_name="Tommy")
    anna = User(wa_user_id=ANNA, display_name="Anna")
    async with sessions() as session:
        session.add_all([tommy, anna])
        await session.flush()
        for owner, other, kind, cents in [
            (tommy, anna, LedgerKind.RECEIVABLE, 1000), (anna, tommy, LedgerKind.PAYABLE, 1000),
            (anna, tommy, LedgerKind.RECEIVABLE, 400), (tommy, anna, LedgerKind.PAYABLE, 400)
        ]:
            session.add(LedgerEntry(
                user_id=owner.id, kind=kind, amount_cents=cents, currency="SGD",
                counterparty_id=other.id
            ))
        await session.commit()
    return tommy, anna


# a button tap from one person
def tap(user: str, button_id: str) -> Inbound:
    return Inbound(f"m-{user}-{button_id}", user, "button", button_id=button_id)


# mutual debts net to one transfer, the message says so and brings the paynow qr
async def test_ask_nets_and_offers_paynow(sessions):
    tommy, anna = await debts(sessions)
    settle = SettleUp(sessions, PayRails((PayNow(),)))
    async with sessions() as session:
        assert await settle.owing(session, anna.id, tommy.id, "SGD") == 600
        assert await settle.owing(session, tommy.id, anna.id, "SGD") == -600
    transport = FakeTransport()
    await settle.ask(transport, anna, tommy, "SGD", "*You owe Tommy SGD 10.00*", 1000, "SL AB")
    to, text, buttons = transport.sent_buttons[-1]
    assert to == ANNA and "With earlier bills that's *SGD 6.00* in total." in text
    assert "*PayNow* to 8123 4567" in text
    assert buttons == [paid_button(tommy.id, "SGD"), OWE_ALL]
    assert transport.sent_images[-1][0] == ANNA


# without a pack anna still gets the amount and i've paid
async def test_ask_without_pack(sessions):
    tommy, anna = await debts(sessions)
    transport = FakeTransport()
    await SettleUp(sessions, PayRails()).ask(transport, anna, tommy, "SGD", "owe", 600, "x")
    assert transport.sent_buttons[-1][1] == f"owe\n\n{PLAIN}"
    assert transport.sent_images == []


# i've paid clears the pair on both sides and tells tommy, only tommy can undo it
async def test_paid_then_undo(sessions):
    tommy, anna = await debts(sessions)
    settle = SettleUp(sessions, PayRails())
    transport = FakeTransport()
    done = paid_button(tommy.id, "SGD")[0]
    await settle.on_button(tap(ANNA, done), done, transport)
    marked = "✅ *Marked as paid · SGD 6.00 to Tommy*\nI've let Tommy know."
    assert transport.sent_text[-1] == (ANNA, marked)
    to, text, buttons = transport.sent_buttons[-1]
    assert to == TOMMY and text.startswith("*Anna paid you SGD 6.00*")
    async with sessions() as session:
        assert await settle.owing(session, anna.id, tommy.id, "SGD") == 0
        assert await settle.owing(session, tommy.id, anna.id, "SGD") == 0
    board = await Overview(sessions, QuotaBook(sessions)).build(tommy.id)
    assert board is not None and board.people() == []

    undo = buttons[0][0]
    await settle.on_button(tap(ANNA, undo), undo, transport)
    assert transport.sent_text[-1] == (ANNA, NOT_YOURS)
    await settle.on_button(tap(TOMMY, undo), undo, transport)
    assert transport.sent_text[-1] == (TOMMY, "Undone. Anna still owes you SGD 6.00.")
    assert transport.sent_buttons[-1][0] == ANNA
    async with sessions() as session:
        assert await settle.owing(session, anna.id, tommy.id, "SGD") == 600
        voided = (await session.execute(
            select(LedgerEntry).where(LedgerEntry.kind == LedgerKind.SETTLEMENT)
        )).scalars().all()
    assert len(voided) == 2 and all(row.voided_at is not None for row in voided)


# the dashboard knows who anna owes so it can offer i've paid, and paying from it settles too
async def test_pay_from_dashboard(sessions):
    tommy, anna = await debts(sessions)
    book = QuotaBook(sessions)
    board = await Overview(sessions, book).build(anna.id)
    assert board is not None
    [owed] = board.people()
    assert (owed.who, owed.cents, owed.user, owed.wa) == ("Tommy", -600, tommy.id, TOMMY)
    transport = FakeTransport()
    settle = SettleUp(sessions, PayRails())
    assert await settle.pay(anna.id, tommy.id, "SGD", transport) == 600
    assert await settle.pay(anna.id, tommy.id, "SGD", transport) == 0
    assert transport.sent_text[-1] == (ANNA, "You're all square with Tommy.")
    board = await Overview(sessions, book).build(anna.id)
    assert board is not None and board.people() == []


# nothing owed means nothing to pay
async def test_paid_when_square(sessions):
    tommy, anna = await debts(sessions)
    settle = SettleUp(sessions, PayRails())
    transport = FakeTransport()
    done = paid_button(anna.id, "SGD")[0]
    await settle.on_button(tap(TOMMY, done), done, transport)
    assert transport.sent_text[-1] == (TOMMY, "You're all square with Anna.")
