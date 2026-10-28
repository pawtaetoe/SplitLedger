from datetime import date

from splitledger.db.models import PlannedSpend
from splitledger.plans.upcoming import Due, add_months, due_days, open_days, outlook, per_month

TODAY = date(2026, 10, 28)


# one active plan
def planned(label: str, cents: int, every: str, due_on: date) -> PlannedSpend:
    return PlannedSpend(label=label, amount_cents=cents, currency="SGD", every=every, due_on=due_on)


# a mix the outlook tests share
PLANS = [
    planned("trip", 50000, "once", date(2026, 10, 29)),
    planned("phone", 5000, "month", date(2026, 9, 30)),
    planned("gym", 2000, "week", date(2026, 10, 2)),
    planned("rent", 120000, "month", date(2026, 10, 1)),
    planned("present", 15000, "once", date(2026, 12, 3)),
    planned("old gift", 9000, "once", date(2026, 10, 1))
]


# month steps keep the day or fall back to the month end
def test_add_months():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2026, 11, 15), 2) == date(2027, 1, 15)


# repeats land on every due day in the window, one offs only once
def test_due_days():
    window = (date(2026, 10, 1), date(2026, 11, 1))
    assert due_days("week", date(2026, 10, 2), *window) == [
        date(2026, 10, 2), date(2026, 10, 9), date(2026, 10, 16), date(2026, 10, 23),
        date(2026, 10, 30)
    ]
    assert due_days("month", date(2026, 1, 31), *window) == [date(2026, 10, 31)]
    assert due_days("year", date(2025, 10, 5), *window) == [date(2026, 10, 5)]
    assert due_days("once", date(2026, 11, 1), *window) == []


# a usual month counts repeats only
def test_per_month():
    assert per_month("week", 1200) == 5200
    assert per_month("year", 12000) == 1000
    assert per_month("once", 5000) == 0


# still to pay this month, one offs after it and the monthly total
def test_outlook():
    ahead = outlook(PLANS, TODAY)
    assert ahead.this_month == [
        Due("trip", 50000, date(2026, 10, 29)),
        Due("phone", 5000, date(2026, 10, 30)),
        Due("gym", 2000, date(2026, 10, 30))
    ]
    assert ahead.due_total() == 57000
    assert ahead.later == [Due("present", 15000, date(2026, 12, 3))]
    assert ahead.monthly == 5000 + 8666 + 120000


# paid due days drop out of the window and out of what is still to pay
def test_paid_days_drop_out():
    phone = planned("phone", 5000, "month", date(2026, 9, 30))
    phone.paid_through = date(2026, 10, 30)
    assert open_days(phone, TODAY, date(2026, 12, 1)) == [date(2026, 11, 30)]
    assert outlook([phone], TODAY).this_month == []
