from zoneinfo import ZoneInfo

from splitledger.db.models import BudgetPlan, LedgerEntry, LedgerKind, User
from splitledger.handlers.ask import SpendTools, owed_text
from splitledger.plans.afford import CORE_RULES
from splitledger.plans.frameworks import PRESETS
from splitledger.plans.quota import QuotaBook
from splitledger.split.finalise import BillCloser
from splitledger.split.overview import Overview
from tests.integration.test_finalise import claimed


# before closing the bill shows as open with progress, after it the balances match the ledger
async def test_overview_follows_the_bill(sessions):
    bill, _, anna = await claimed(sessions)
    overview = Overview(sessions, QuotaBook(sessions))
    before = await overview.build(bill.payer_id)
    assert before is not None
    assert [(row.bill.code, row.claimed, row.items, row.people) for row in before.paid] == [
        ("K7Q2AB", 3, 3, 3)
    ]
    friend = await overview.build(anna.id)
    assert friend is not None and [row.bill.code for row in friend.joined] == ["K7Q2AB"]
    # before closing nothing is owed, but each side sees the shares so far
    assert (before.paid[0].mine, before.paid[0].others) == (2414, 4586)
    assert friend.joined[0].mine == 2414 and friend.pending() == [("SGD", 2414)]
    assert friend.joined[0].payer_name == "Tommy"
    assert friend.people() == []
    await BillCloser(sessions).close(bill.id)
    after = await overview.build(bill.payer_id)
    assert after is not None and after.paid == []
    assert [(b.who, b.cents) for b in after.owed_to_me] == [("Anna", 2414), ("Ben", 2172)]
    assert after.month == [("Other", 2414)] and after.month_total() == 2414
    friend = await overview.build(anna.id)
    assert friend is not None and [(b.who, b.cents) for b in friend.i_owe] == [("Tommy", 2414)]
    # one net line per person, positive owes you and negative you owe
    assert [(b.who, b.cents) for b in after.people()] == [("Anna", 2414), ("Ben", 2172)]
    assert after.net() == 4586
    assert [(b.who, b.cents) for b in friend.people()] == [("Tommy", -2414)]
    assert [entry.amount_cents for entry in friend.recent] == [2414]


# the assistant answers who owes whom from the same numbers, each in its own currency
async def test_balances_tool(sessions):
    bill, _, anna = await claimed(sessions)
    await BillCloser(sessions).close(bill.id)
    zone = ZoneInfo("Asia/Singapore")
    friend = SpendTools(sessions, anna.id, zone, QuotaBook(sessions), CORE_RULES)
    answer = await friend.balances({})
    assert answer["you_owe"] == [{"who": "Tommy", "amount": "SGD 24.14"}]
    assert answer["they_owe_you"] == [] and answer["open_bills_you_are_in"] == []
    payer = SpendTools(sessions, bill.payer_id, zone, QuotaBook(sessions), CORE_RULES)
    owed = (await payer.balances({}))["they_owe_you"]
    assert owed == [{"who": "Anna", "amount": "SGD 24.14"}, {"who": "Ben", "amount": "SGD 21.72"}]



# the owed list reads the same numbers as the dashboard
async def test_owed_text(sessions):
    bill, _, anna = await claimed(sessions)
    before = await owed_text(sessions, QuotaBook(sessions), anna.id)
    assert before.startswith("*Open bills, not owed yet*")
    await BillCloser(sessions).close(bill.id)
    after = await owed_text(sessions, QuotaBook(sessions), anna.id)
    assert after == "*You owe*\n- Tommy *SGD 24.14*"



# spending in another currency shows on its own and never mixes into the main totals
async def test_other_currencies_kept_apart(sessions):
    bill, _, _ = await claimed(sessions)
    async with sessions() as session:
        session.add(LedgerEntry(
            user_id=bill.payer_id, kind=LedgerKind.EXPENSE, amount_cents=12400, currency="CHF",
            category="Dining"
        ))
        await session.commit()
    board = await Overview(sessions, QuotaBook(sessions)).build(bill.payer_id)
    assert board is not None and board.month == [] and board.month_total() == 0
    assert board.abroad == [("Dining", "CHF", 12400)]
    assert board.other_totals() == [("CHF", 12400)]


# fixed rates for tests, one franc is one and a half dollars
class FakeRates:
    # the same rates every time
    async def to(self, base: str) -> dict[str, float]:
        return {"SGD": 1.0, "CHF": 1.5}


# with a rate, other currencies fold into the main total and the split is kept for the accordion
async def test_other_currencies_combined(sessions):
    bill, _, _ = await claimed(sessions)
    async with sessions() as session:
        session.add_all([
            LedgerEntry(user_id=bill.payer_id, kind=LedgerKind.EXPENSE, amount_cents=12400,
                        currency="CHF", category="Dining"),
            LedgerEntry(user_id=bill.payer_id, kind=LedgerKind.EXPENSE, amount_cents=11940,
                        currency="SGD", category="Dining")
        ])
        await session.commit()
    board = await Overview(sessions, QuotaBook(sessions), FakeRates()).build(bill.payer_id)
    assert board is not None and board.month == [("Dining", 11940 + 18600)]
    assert sorted(board.parts["Dining"]) == [("CHF", 12400, 18600), ("SGD", 11940, 11940)]
    assert board.converted() and board.abroad == []


# the plan counts spending abroad at today's rate, and skips what has no rate
async def test_plan_counts_other_currencies(sessions):
    user = User(wa_user_id="6597777777", currency="SGD", timezone="Asia/Singapore",
                take_home_cents=450000)
    async with sessions() as session:
        session.add(user)
        await session.flush()
        fifty = PRESETS["503020"]
        session.add(BudgetPlan(
            user_id=user.id, framework="503020", period="monthly", buckets=fifty.buckets,
            category_map=fifty.category_map, caps={}
        ))
        session.add_all([
            LedgerEntry(user_id=user.id, kind=LedgerKind.EXPENSE, amount_cents=amount,
                        currency=code, category="Dining")
            for amount, code in [(12400, "CHF"), (1000, "SGD"), (5000, "VND")]
        ])
        await session.commit()
    report = await QuotaBook(sessions, FakeRates()).report(user.id)
    assert report is not None
    wants = next(b for b in report.buckets if b.name == "Wants")
    assert wants.spent_cents == 18600 + 1000
