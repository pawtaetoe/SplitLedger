from calendar import monthrange
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta

from splitledger.db.models import PlannedSpend

EVERY = ("once", "week", "month", "year")
# days ahead that one off plans still count as coming up
AHEAD_DAYS = 60


# the same day n months later, moved back to the month end when it is shorter
def add_months(day: date, n: int) -> date:
    index = day.year * 12 + day.month - 1 + n
    year, month = index // 12, index % 12 + 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


# the first day of the next month
def next_month(day: date) -> date:
    return add_months(day.replace(day=1), 1)


# every day a plan falls due from start up to before end
def due_days(every: str, first: date, start: date, end: date) -> list[date]:
    if every == "once":
        return [first] if start <= first < end else []
    days: list[date] = []
    n = 0
    day = first
    while day < end:
        if day >= start:
            days.append(day)
        n += 1
        day = first + timedelta(weeks=n) if every == "week" else add_months(
            first, n * (12 if every == "year" else 1)
        )
    return days


# due days of one plan in a window that are not paid yet
def open_days(plan: PlannedSpend, start: date, end: date) -> list[date]:
    paid = plan.paid_through
    days = due_days(plan.every, plan.due_on, start, end)
    return [day for day in days if paid is None or day > paid]


# what a repeating plan costs in a usual month, a one off costs nothing there
def per_month(every: str, cents: int) -> int:
    return {"week": cents * 52 // 12, "month": cents, "year": cents // 12}.get(every, 0)


# one payment expected on a day
@dataclass(frozen=True)
class Due:
    label: str
    amount: int
    on: date


# what is still to pay this month, one offs after it and repeats in a usual month
@dataclass(frozen=True)
class Outlook:
    this_month: list[Due] = field(default_factory=list)
    later: list[Due] = field(default_factory=list)
    monthly: int = 0

    # total still to pay this month
    def due_total(self) -> int:
        return sum(due.amount for due in self.this_month)


# the outlook from today for the plans still active
def outlook(plans: Iterable[PlannedSpend], today: date) -> Outlook:
    month_end = next_month(today)
    horizon = today + timedelta(days=AHEAD_DAYS)
    this_month: list[Due] = []
    later: list[Due] = []
    monthly = 0
    for plan in plans:
        this_month += [
            Due(plan.label, plan.amount_cents, day)
            for day in open_days(plan, today, month_end)
        ]
        if plan.every == "once":
            later += [
                Due(plan.label, plan.amount_cents, day)
                for day in open_days(plan, month_end, horizon)
            ]
        monthly += per_month(plan.every, plan.amount_cents)
    return Outlook(
        sorted(this_month, key=lambda d: d.on), sorted(later, key=lambda d: d.on), monthly
    )
