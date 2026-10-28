from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from splitledger.handlers.log import money
from splitledger.plans.quota import PlanReport
from splitledger.plans.upcoming import Due, Outlook

KINDS = ("purchase", "car", "home")
PROPERTY_TYPES = ("hdb", "ec", "private", "other")


# whole cents from a plain amount the model passed, none when missing
def to_cents(value: Any) -> int | None:
    if value is None:
        return None
    cents = int(Decimal(str(value)) * 100)
    if cents < 0:
        raise ValueError("amounts can't be negative")
    return cents


# smallest income where cost is at most pct percent of it, rounded up to the cent
def income_needed(cost_cents: int, pct: int) -> int:
    return -(-cost_cents * 100 // pct)


# what the user asked about, in cents and months
@dataclass(frozen=True)
class Ask:
    kind: str
    price: int | None = None
    down: int | None = None
    monthly: int | None = None
    running: int | None = None
    term_months: int | None = None
    other_debt: int | None = None
    property_type: str | None = None
    omv: int | None = None
    category: str | None = None

    # down payment from an amount or a percent of the price
    @staticmethod
    def from_args(args: dict[str, Any]) -> "Ask":
        kind = str(args.get("kind", ""))
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        price = to_cents(args.get("price"))
        down = to_cents(args.get("down_payment"))
        pct = args.get("down_payment_pct")
        if down is None and pct is not None and price is not None:
            down = price * int(Decimal(str(pct))) // 100
        term = args.get("term_months")
        prop = args.get("property_type")
        if prop is not None and prop not in PROPERTY_TYPES:
            raise ValueError(f"property_type must be one of {', '.join(PROPERTY_TYPES)}")
        return Ask(
            kind, price, down, to_cents(args.get("monthly_payment")),
            to_cents(args.get("running_costs")), int(term) if term is not None else None,
            to_cents(args.get("other_monthly_debt")), prop, to_cents(args.get("omv")),
            args.get("category")
        )


# what the user spends, this month and in a usual month
@dataclass(frozen=True)
class Spending:
    this_month: int
    category_this_month: int
    month_average: int
    category_average: int
    months: int


# the money the user has to measure against
@dataclass(frozen=True)
class Means:
    currency: str
    take_home: int | None
    gross: int | None
    report: PlanReport | None
    spending: Spending | None = None
    outlook: Outlook | None = None

    # gross pay when known, else take home pay which is the safer side
    def pay(self) -> tuple[int | None, str]:
        if self.gross:
            return self.gross, "gross pay"
        return self.take_home, "take-home pay, so this is on the safe side"


# the outcome of one check, ok none means we can't tell yet
@dataclass(frozen=True)
class Verdict:
    rule: str
    source: str
    ok: bool | None
    need: int | None = None
    on: str | None = None
    note: str = ""


Rule = Callable[[Ask, Means], list[Verdict]]


# a share of income check, pass when income covers cost at that percent
def share_check(
    rule: str, source: str, cost: int, pct: int, income: int | None, on: str, note: str = ""
) -> Verdict:
    need = income_needed(cost, pct)
    ok = None if income is None else income >= need
    return Verdict(rule, source, ok, need, on, note)


# a one off purchase against what is left in its plan bucket this period
def purchase_rule(ask: Ask, means: Means) -> list[Verdict]:
    if ask.kind != "purchase" or ask.price is None:
        return []
    source = "your budget plan"
    report = means.report
    # without a plan part to check against the facts speak instead
    if report is None:
        return []
    name = report.plan.category_map.get(ask.category or "Other")
    bucket = next((b for b in report.buckets if b.name == name), None)
    if bucket is None:
        return []
    left = bucket.left_cents()
    note = f"{money(max(left, 0), means.currency)} left in {bucket.name} this {report.unit()}"
    if left < ask.price:
        spare = [
            b.name for b in report.buckets
            if b is not bucket and not b.saving and b.left_cents() >= ask.price - max(left, 0)
        ]
        if spare:
            note += f"; the gap fits in what is left of {spare[0]}"
        else:
            note += f"; it would fit next {report.unit()}"
    return [Verdict(f"{bucket.name} this {report.unit()}", source, left >= ask.price, note=note)]


# 20 4 10, at least 20 percent down, at most 4 years, transport at most 10 percent of income
def car_20_4_10(ask: Ask, means: Means) -> list[Verdict]:
    if ask.kind != "car":
        return []
    source = "the 20/4/10 car rule (J.D. Power)"
    verdicts = []
    if ask.price is not None and ask.down is not None:
        ok = ask.down * 100 >= ask.price * 20
        verdicts.append(Verdict("20/4/10: at least 20% down", source, ok))
    if ask.term_months is not None:
        verdicts.append(Verdict("20/4/10: loan of 4 years or less", source, ask.term_months <= 48))
    if ask.monthly:
        cost = ask.monthly + (ask.running or 0)
        note = "" if ask.running else "payment only, insurance, fuel and parking would add to it"
        verdicts.append(share_check(
            "20/4/10: car costs at most 10% of pay", source, cost, 10, means.take_home,
            "take-home pay", note
        ))
    return verdicts


# 28 36, housing at most 28 percent and all debt at most 36 percent of gross pay
def home_28_36(ask: Ask, means: Means) -> list[Verdict]:
    if ask.kind != "home" or not ask.monthly:
        return []
    source = "the 28/36 home rule (Chase)"
    income, on = means.pay()
    total = ask.monthly + (ask.other_debt or 0)
    return [
        share_check("28/36: housing at most 28% of pay", source, ask.monthly, 28, income, on),
        share_check("28/36: all debt at most 36% of pay", source, total, 36, income, on)
    ]


CORE_RULES: list[Rule] = [purchase_rule, car_20_4_10, home_28_36]


# whole percent of a whole, with under 1 for tiny shares
def share_text(part: int, whole: int) -> str:
    pct = part * 100 // whole
    return "under 1%" if pct < 1 else f"about {pct}%"


# up to three labels, the rest counted
def names(dues: list[Due]) -> str:
    labels = list(dict.fromkeys(due.label for due in dues))
    shown = ", ".join(labels[:3])
    return shown if len(labels) <= 3 else f"{shown} and {len(labels) - 3} more"


# plain facts about what a purchase means for the user, so they can decide
def purchase_facts(ask: Ask, means: Means) -> list[str]:
    if ask.kind != "purchase" or not ask.price:
        return []
    c = means.currency
    price = money(ask.price, c)
    pay = means.take_home
    spending = means.spending
    ahead = means.outlook or Outlook()
    facts = []
    if pay:
        facts.append(f"{price} is {share_text(ask.price, pay)} of a month's pay ({money(pay, c)}).")
    if spending is not None:
        facts.append(f"Spent so far this month: {money(spending.this_month, c)}.")
    if ahead.this_month:
        due = money(ahead.due_total(), c)
        facts.append(f"Still to pay this month: {due} for {names(ahead.this_month)}.")
    if pay and spending is not None:
        left = pay - spending.this_month - ahead.due_total()
        after = "Left this month after bills" if ahead.this_month else "Left this month"
        if left <= 0:
            facts.append("Spending and bills already use all of this month's pay.")
        elif left < ask.price:
            facts.append(f"{after}: {money(left, c)}, {money(ask.price - left, c)} short of this.")
        else:
            facts.append(f"{after}: {money(left, c)}. This is {share_text(ask.price, left)} of it.")
    if ahead.monthly:
        facts.append(f"Repeating bills come to about {money(ahead.monthly, c)} a month.")
    if ahead.later:
        coming = "; ".join(
            f"{due.label} {money(due.amount, c)} on {due.on.day} {due.on:%b}"
            for due in ahead.later[:2]
        )
        facts.append(f"Coming up later: {coming}.")
    if spending is not None and spending.months:
        facts.append(f"A usual month costs you {money(spending.month_average, c)}.")
    if spending is not None and ask.category:
        typical = money(spending.category_average, c)
        usual = f", against a usual {typical}" if spending.months else ""
        so_far = money(spending.category_this_month, c)
        facts.append(f"So far this month: {so_far} on {ask.category.lower()}{usual}.")
    if spending is not None and not spending.months:
        facts.append("There isn't a full month of history yet, so these are early numbers.")
    if not pay:
        facts.append("Add your monthly pay with /plan to see this against your income.")
    return facts


# run every rule that applies and sum it up for the model to explain
def assess(ask: Ask, means: Means, rules: list[Rule]) -> dict[str, Any]:
    verdicts = [v for rule in rules for v in rule(ask, means)]
    facts = purchase_facts(ask, means)
    if not verdicts and facts:
        return {"answer": "your call", "facts": facts}
    if not verdicts:
        return {"answer": "not sure", "why": "give a price or a monthly payment to check"}
    answer = "no" if any(v.ok is False for v in verdicts) else (
        "not sure" if any(v.ok is None for v in verdicts) else "yes"
    )
    needs = [v for v in verdicts if v.need is not None]
    strictest = max(needs, key=lambda v: v.need or 0) if needs else None
    result: dict[str, Any] = {
        "answer": answer,
        "checks": [
            {
                "rule": v.rule,
                "source": v.source,
                "result": {True: "pass", False: "fail", None: "unknown"}[v.ok],
                **({"needs": money(v.need, means.currency), "on": v.on} if v.need else {}),
                **({"note": v.note} if v.note else {})
            }
            for v in verdicts
        ]
    }
    if facts:
        result["facts"] = facts
    if strictest is not None and strictest.need is not None:
        result["strictest"] = strictest.rule
        result["income_needed"] = money(strictest.need, means.currency)
    if means.take_home:
        result["their_take_home"] = money(means.take_home, means.currency)
    if not means.take_home and not means.gross:
        result["missing"] = "their monthly pay, ask them to include it or set it with /plan"
    return result
