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
