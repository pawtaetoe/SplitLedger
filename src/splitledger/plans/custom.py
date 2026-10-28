from dataclasses import dataclass, field
from typing import get_args

from splitledger.llm.schemas import Category, CustomPlan
from splitledger.plans.frameworks import Framework

MAX_BUCKETS = 6
MAX_NAME = 20
CATEGORIES = list(get_args(Category))


# a drafted plan after checking, a framework to save or a problem to show
@dataclass(frozen=True)
class Checked:
    framework: Framework | None
    problem: str | None = None
    notes: list[str] = field(default_factory=list)


# a limit stated per week as a monthly amount, a year of weeks over twelve months
def monthly_cap(cents: int, per: str | None) -> int:
    return cents * 52 // 12 if per == "week" else cents


# turn what the model drafted into a plan, or say plainly what is wrong with it
def check_plan(plan: CustomPlan) -> Checked:
    if not 1 <= len(plan.buckets) <= MAX_BUCKETS:
        return Checked(None, f"A plan needs 1 to {MAX_BUCKETS} parts.")
    buckets: dict[str, int] = {}
    caps: dict[str, int] = {}
    mapping: dict[str, str] = {}
    notes: list[str] = []
    for bucket in plan.buckets:
        name = " ".join(bucket.name.split())[:MAX_NAME]
        if not name or name in buckets:
            return Checked(None, "Each part needs its own short name.")
        if (bucket.pct is None) == (bucket.cap_cents is None):
            return Checked(None, f"Give {name} either a share of your pay or a money limit.")
        if bucket.pct is not None and not 0 < bucket.pct <= 100:
            return Checked(None, f"{name} needs a share between 1% and 100%.")
        if bucket.cap_cents is not None and (bucket.cap_cents <= 0 or not bucket.categories):
            return Checked(None, f"Say what {name} is for and how much, like food under 400.")
        buckets[name] = bucket.pct or 0
        if bucket.cap_cents is not None:
            caps[name] = monthly_cap(bucket.cap_cents, bucket.cap_per)
        for category in bucket.categories:
            if category not in CATEGORIES:
                notes.append(f"I don't track {category.lower()}, so I left it out.")
            elif category in mapping:
                notes.append(f"{category} is in two parts, so I kept it in {mapping[category]}.")
            else:
                mapping[category] = name
    total = sum(buckets.values())
    if total > 100:
        return Checked(None, f"The shares add up to {total}%. Keep them at 100% or less.")
    untracked = [c.lower() for c in CATEGORIES if c not in mapping]
    if untracked:
        notes.append("Not tracked: " + ", ".join(untracked) + ".")
    framework = Framework("custom", "Your own plan", "", "", buckets, mapping, caps)
    return Checked(framework, notes=notes)


# true when the shares and limits fit inside the monthly take home pay
def fits_income(framework: Framework, take_home_cents: int) -> bool:
    shares = take_home_cents * sum(framework.buckets.values()) // 100
    return shares + sum(framework.caps.values()) <= take_home_cents
