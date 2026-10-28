import uuid

from sqlalchemy import func, select

from splitledger.contracts.messages import Inbound
from splitledger.db.models import Allocation, Bill, LedgerEntry, LedgerKind, Participant, User
from splitledger.handlers import bill as bill_module
from splitledger.handlers.bill import GroupBills
from splitledger.handlers.log import ExpenseLogger
from splitledger.llm.schemas import GLOBAL
from splitledger.split.claims import ClaimBook
from splitledger.split.finalise import BillCloser
from splitledger.split.tokens import ClaimTokens
from tests.fakes import FakeLLM, FakeTransport
from tests.integration.test_claims import seed

PAYER = "6581234567"
ANNA = "6591111111"


# anna has an account and claims a pasta, ben is a web guest with the calamari
async def claimed(sessions) -> tuple[Bill, ClaimBook, User]:
    bill, items, me = await seed(sessions)
    book = ClaimBook(sessions)
    anna = User(wa_user_id=ANNA, currency="SGD", timezone="Asia/Singapore")
    async with sessions() as session:
        session.add(anna)
        await session.commit()
    anna_id = await book.person_for(bill.id, anna.id, "Anna")
    ben_id = await book.join(bill.id, "Ben")
    await book.claim(bill.id, me.id, items[0].id, "mine")
    await book.claim(bill.id, anna_id, items[1].id, "mine")
    await book.claim(bill.id, ben_id, items[2].id, "mine")
    return bill, book, anna


# total cents of one kind for one user
async def total(sessions, user_id: uuid.UUID, kind: LedgerKind) -> int:
    async with sessions() as session:
        query = select(func.coalesce(func.sum(LedgerEntry.amount_cents), 0)).where(
            LedgerEntry.user_id == user_id, LedgerEntry.kind == kind
        )
        return int((await session.execute(query)).scalar_one())


# receivables equal linked payables plus unlinked allocations, and a second close posts nothing
async def test_close_posts_once(sessions):
    bill, _, anna = await claimed(sessions)
    closer = BillCloser(sessions)
    closing = await closer.close(bill.id)
    assert closing is not None
    assert sum(share.cents for share in closing.shares) == 7000
    async with sessions() as session:
        unlinked = (await session.execute(
            select(func.sum(Allocation.amount_cents)).where(Allocation.posted_at.is_(None))
        )).scalar_one()
        rows = (await session.execute(select(func.count()).select_from(LedgerEntry))).scalar_one()
    receivable = await total(sessions, bill.payer_id, LedgerKind.RECEIVABLE)
    payable = await total(sessions, anna.id, LedgerKind.PAYABLE)
    assert receivable == payable + unlinked == 2414 + 2172
    # budgets only see the payer's own share, never what friends owe
    assert await total(sessions, bill.payer_id, LedgerKind.EXPENSE) == 2414
    assert await total(sessions, anna.id, LedgerKind.EXPENSE) == 2414
    assert await closer.close(bill.id) is None
    async with sessions() as session:
        again = (await session.execute(select(func.count()).select_from(LedgerEntry))).scalar_one()
    assert again == rows


# a guest who links after the close gets their rows once
async def test_late_link_posts_once(sessions):
    bill, book, _ = await claimed(sessions)
    await BillCloser(sessions).close(bill.id)
    ben = User(wa_user_id="6592222222", currency="SGD", timezone="Asia/Singapore")
    async with sessions() as session:
        session.add(ben)
        await session.commit()
    await book.person_for(bill.id, ben.id, "Ben")
    await book.person_for(bill.id, ben.id, "Ben")
    assert await total(sessions, ben.id, LedgerKind.PAYABLE) == 2172
    assert await total(sessions, ben.id, LedgerKind.EXPENSE) == 2172


# only the payer may close, leftovers are asked about, and friends hear what they owe
async def test_close_by_buttons(sessions):
    bill, items, _ = await seed(sessions)
    book = ClaimBook(sessions)
    anna = User(wa_user_id=ANNA, currency="SGD", timezone="Asia/Singapore")
    async with sessions() as session:
        session.add(anna)
        await session.commit()
    anna_id = await book.person_for(bill.id, anna.id, "Anna")
    await book.claim(bill.id, anna_id, items[0].id, "mine")
    logger = ExpenseLogger(FakeLLM(), sessions, GLOBAL)
    group = GroupBills(sessions, logger, GLOBAL, ClaimTokens("s"), "https://sl.test", "1")
    transport = FakeTransport()
    close = f"bill:close:{bill.code}"
    await group.on_button(Inbound("m1", ANNA, "button", button_id=close), close, transport)
    assert transport.sent_text[-1][1] == bill_module.NOT_PAYER
    await group.on_button(Inbound("m2", PAYER, "button", button_id=close), close, transport)
    _, ask, buttons = transport.sent_buttons[-1]
    assert ask.startswith("*Nobody claimed Pasta, Calamari*")
    rest = buttons[1][0]
    await group.on_button(Inbound("m3", PAYER, "button", button_id=rest), rest, transport)
    sent = dict((to, body) for to, body in transport.sent_text[-2:])
    assert sent[PAYER].startswith("*Bill closed · Bill*\nYou paid SGD 70.00.")
    assert "- Anna *SGD 24.14*" in sent[PAYER]
    assert sent[ANNA].startswith("*You owe Tommy SGD 24.14*")
    async with sessions() as session:
        people = (await session.execute(select(func.count()).select_from(Participant))).scalar_one()
    assert people == 2
