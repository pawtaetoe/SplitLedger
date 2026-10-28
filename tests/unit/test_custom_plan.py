import pytest

from splitledger.llm.schemas import BucketDraft, CustomPlan
from splitledger.plans.custom import check_plan, fits_income, monthly_cap
from splitledger.plans.quota import bucket_quota


# a plan with these parts
def plan(*buckets: BucketDraft) -> CustomPlan:
    return CustomPlan(buckets=list(buckets))


# shares and limits become a framework with categories mapped and the rest listed
def test_valid_plan():
    checked = check_plan(plan(
        BucketDraft(name="Food", cap_cents=10000, cap_per="week", categories=["Dining"]),
        BucketDraft(name="Savings", pct=30)
    ))
    framework = checked.framework
    assert framework is not None
    assert framework.buckets == {"Food": 0, "Savings": 30}
    assert framework.caps == {"Food": 43333}
    assert framework.category_map == {"Dining": "Food"}
    assert framework.savings() == ["Savings"]
    assert checked.notes[-1].startswith("Not tracked: groceries, transport")


# plans that cannot work are refused with a plain reason
@pytest.mark.parametrize(
    ("buckets", "reason"),
    [
        ([], "1 to 6 parts"),
        ([BucketDraft(name="A", pct=70), BucketDraft(name="B", pct=50)], "add up to 120%"),
        ([BucketDraft(name="A", pct=10, cap_cents=100, categories=["Dining"])], "either a share"),
        ([BucketDraft(name="A")], "either a share"),
        ([BucketDraft(name="Food", cap_cents=40000)], "what Food is for"),
        ([BucketDraft(name="A", pct=0)], "between 1% and 100%"),
        ([BucketDraft(name="A", pct=10), BucketDraft(name=" A ", pct=10)], "own short name")
    ]
)
def test_refused(buckets, reason):
    checked = check_plan(CustomPlan(buckets=buckets))
    assert checked.framework is None
    assert reason in (checked.problem or "")


# unknown categories are dropped and a category in two parts stays in the first
def test_notes_for_odd_categories():
    checked = check_plan(plan(
        BucketDraft(name="Fun", pct=10, categories=["Dining", "Pets"]),
        BucketDraft(name="Food", pct=10, categories=["Dining", "Groceries"])
    ))
    assert checked.framework is not None
    assert checked.framework.category_map == {"Dining": "Fun", "Groceries": "Food"}
    assert "I don't track pets, so I left it out." in checked.notes
    assert "Dining is in two parts, so I kept it in Fun." in checked.notes


# weekly limits scale to months and back without drifting
def test_limits_and_income():
    assert monthly_cap(10000, "week") == 43333
    assert monthly_cap(40000, "month") == 40000
    assert bucket_quota(450000, 0, 40000, "monthly") == 40000
    assert bucket_quota(450000, 0, 43333, "weekly") == 9999
    checked = check_plan(plan(
        BucketDraft(name="Food", cap_cents=200000, categories=["Dining"]),
        BucketDraft(name="Savings", pct=60)
    ))
    assert checked.framework is not None
    assert fits_income(checked.framework, 500000)
    assert not fits_income(checked.framework, 450000)
