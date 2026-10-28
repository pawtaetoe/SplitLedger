import pytest

from splitledger.db.models import BudgetPlan, User
from splitledger.plans import afford
from splitledger.plans.afford import CORE_RULES, Ask, Means, Spending, assess
from splitledger.plans.afford_sg import SG_RULES
from splitledger.plans.quota import BucketStatus, PlanReport
from splitledger.plans.upcoming import outlook
from tests.unit.test_upcoming import PLANS, TODAY

SG_ALL = [*CORE_RULES, *SG_RULES]


# pay only, no plan
def pay(take_home: int | None = 450000, gross: int | None = None) -> Means:
    return Means("SGD", take_home, gross, None)


# the result of one named check
def check(result: dict, starts: str) -> dict:
    return next(c for c in result["checks"] if c["rule"].startswith(starts))


# rounding up to the cent and plain amounts to cents
def test_helpers():
    assert afford.income_needed(150000, 10) == 1500000
    assert afford.income_needed(90000, 55) == 163637
    assert afford.to_cents(12.5) == 1250 and afford.to_cents(None) is None
    with pytest.raises(ValueError):
        afford.to_cents(-1)


# the worked car example: 900 a month plus 600 to run needs 15000 under 20/4/10
def test_car_worked_example():
    ask = Ask.from_args({
        "kind": "car", "price": 100000, "down_payment_pct": 30, "monthly_payment": 900,
        "running_costs": 600, "term_months": 84
    })
    core = assess(ask, pay(), CORE_RULES)
    assert core["answer"] == "no"
    assert core["income_needed"] == "SGD 15000.00"
    assert core["strictest"] == "20/4/10: car costs at most 10% of pay"
    assert check(core, "20/4/10: at least 20% down")["result"] == "pass"
    assert check(core, "20/4/10: loan of 4 years")["result"] == "fail"
    singapore = assess(ask, pay(), SG_ALL)
    down = check(singapore, "MAS: at least 40% down")
    assert down["result"] == "fail" and "OMV above S$20,000" in down["note"]
    assert check(singapore, "MAS: car loan of 7 years")["result"] == "pass"
    assert check(singapore, "MAS TDSR")["needs"] == "SGD 1636.37"


# a cheap car gets the 70 percent loan, so 30 percent down passes
def test_car_low_omv():
    ask = Ask.from_args({"kind": "car", "price": 60000, "down_payment": 18000, "omv": 15000})
    assert check(assess(ask, pay(), SG_ALL), "MAS: at least 30% down")["result"] == "pass"


# a payment alone notes that running costs would add to it
def test_car_payment_only_note():
    result = assess(Ask.from_args({"kind": "car", "monthly_payment": 300}), pay(), CORE_RULES)
    assert result["answer"] == "yes"
    assert "insurance" in check(result, "20/4/10: car costs")["note"]


# homes use gross pay when known, and the hdb loan limit only applies in singapore
def test_home_rules():
    ask = Ask.from_args({
        "kind": "home", "monthly_payment": 2000, "other_monthly_debt": 500, "property_type": "hdb"
    })
    result = assess(ask, pay(gross=800000), SG_ALL)
    assert check(result, "28/36: housing")["needs"] == "SGD 7142.86"
    assert check(result, "28/36: all debt")["needs"] == "SGD 6944.45"
    assert check(result, "MAS MSR")["needs"] == "SGD 6666.67"
    assert check(result, "MAS TDSR")["needs"] == "SGD 4545.46"
    assert result["answer"] == "yes"
    assert result["strictest"] == "28/36: housing at most 28% of pay"
    assert "MAS" not in str(assess(ask, pay(gross=800000), CORE_RULES))


# without gross pay the check uses take home and says it is the safe side
def test_home_take_home_fallback():
    result = assess(Ask.from_args({"kind": "home", "monthly_payment": 2000}), pay(), CORE_RULES)
    assert "safe side" in check(result, "28/36: housing")["on"]


# no pay at all leaves the answer open and says what is missing
def test_missing_pay():
    result = assess(Ask.from_args({"kind": "car", "monthly_payment": 900}), pay(None), CORE_RULES)
    assert result["answer"] == "not sure" and "monthly pay" in result["missing"]


# a plan report with wants and needs this week
def report(wants_spent: int) -> PlanReport:
    plan = BudgetPlan(
        framework="503020", period="weekly",
        buckets={"Needs": 50, "Wants": 30, "Savings": 20},
        category_map={"Travel": "Wants", "Bills": "Needs"}
    )
    user = User(wa_user_id="1", currency="SGD", timezone="Asia/Singapore")
    buckets = [
        BucketStatus("Needs", 51923, 10000, False),
        BucketStatus("Wants", 31153, wants_spent, False),
        BucketStatus("Savings", 20769, 0, True)
    ]
    return PlanReport(plan, user, None, buckets)


# a purchase fits what is left in its bucket, or says where the gap could come from
def test_purchase_against_plan():
    ask = Ask.from_args({"kind": "purchase", "price": 300, "category": "Travel"})
    assert assess(ask, Means("SGD", 450000, None, report(0)), CORE_RULES)["answer"] == "yes"
    tight = assess(ask, Means("SGD", 450000, None, report(20000)), CORE_RULES)
    assert tight["answer"] == "no"
    assert "SGD 111.53 left in Wants this week" in tight["checks"][0]["note"]
    assert "Needs" in tight["checks"][0]["note"]
    assert assess(ask, pay(), CORE_RULES)["answer"] == "your call"


# made up kinds and property types are refused
def test_bad_args():
    with pytest.raises(ValueError):
        Ask.from_args({"kind": "boat"})
    with pytest.raises(ValueError):
        Ask.from_args({"kind": "home", "property_type": "castle"})



# with no plan for it, a purchase gets facts about pay and usual spending and no verdict
def test_purchase_facts_without_plan():
    ask = Ask.from_args({"kind": "purchase", "price": 300, "category": "Travel"})
    spending = Spending(this_month=120000, category_this_month=5000, month_average=320000,
                        category_average=20000, months=3)
    result = assess(ask, Means("SGD", 450000, None, None, spending), CORE_RULES)
    assert result["answer"] == "your call"
    assert result["facts"] == [
        "SGD 300.00 is about 6% of a month's pay (SGD 4500.00).",
        "Spent so far this month: SGD 1200.00.",
        "Left this month: SGD 3300.00. This is about 9% of it.",
        "A usual month costs you SGD 3200.00.",
        "So far this month: SGD 50.00 on travel, against a usual SGD 200.00."
    ]


# bills still due this month come off what is left, repeats and later one offs are named
def test_purchase_facts_with_upcoming():
    ask = Ask.from_args({"kind": "purchase", "price": 300})
    spending = Spending(this_month=120000, category_this_month=0, month_average=0,
                        category_average=0, months=0)
    ahead = outlook(PLANS, TODAY)
    facts = assess(ask, Means("SGD", 450000, None, None, spending, ahead), CORE_RULES)["facts"]
    assert facts[:6] == [
        "SGD 300.00 is about 6% of a month's pay (SGD 4500.00).",
        "Spent so far this month: SGD 1200.00.",
        "Still to pay this month: SGD 570.00 for trip, phone, gym.",
        "Left this month after bills: SGD 2730.00. This is about 10% of it.",
        "Repeating bills come to about SGD 1336.66 a month.",
        "Coming up later: present SGD 150.00 on 3 Dec."
    ]
    tight = assess(ask, Means("SGD", 200000, None, None, spending, ahead), CORE_RULES)["facts"]
    assert "Left this month after bills: SGD 230.00, SGD 70.00 short of this." in tight


# new users get early numbers and a nudge to add their pay
def test_purchase_facts_new_user():
    ask = Ask.from_args({"kind": "purchase", "price": 300, "category": "Travel"})
    spending = Spending(this_month=5000, category_this_month=0, month_average=0,
                        category_average=0, months=0)
    facts = assess(ask, Means("SGD", None, None, None, spending), CORE_RULES)["facts"]
    assert "So far this month: SGD 0.00 on travel." in facts
    assert "There isn't a full month of history yet, so these are early numbers." in facts
    assert facts[-1].startswith("Add your monthly pay with /plan")
