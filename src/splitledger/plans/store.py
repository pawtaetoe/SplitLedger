import uuid

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from splitledger.db.models import BudgetPlan, User
from splitledger.plans.frameworks import Framework
from splitledger.plans.quota import bucket_quota


# each part's amount for one period on a monthly take home pay
def plan_amounts(
    framework: Framework, take_home_cents: int, period: str
) -> list[tuple[str, int, bool]]:
    saving = set(framework.savings())
    return [
        (name, bucket_quota(take_home_cents, pct, framework.caps.get(name), period), name in saving)
        for name, pct in framework.buckets.items()
    ]


# store a plan and the pay it uses, replacing any earlier plan, the caller commits
async def save_plan(
    session: AsyncSession, user_id: uuid.UUID, framework: Framework, period: str,
    take_home_cents: int
) -> None:
    values = {
        "framework": framework.key,
        "period": period,
        "buckets": framework.buckets,
        "category_map": framework.category_map,
        "caps": framework.caps,
        "step_up_pct": 1 if framework.key == "pyf" else None
    }
    await session.execute(
        update(User).where(User.id == user_id).values(take_home_cents=take_home_cents)
    )
    await session.execute(
        insert(BudgetPlan)
        .values(user_id=user_id, **values)
        .on_conflict_do_update(index_elements=["user_id"], set_=values)
    )
