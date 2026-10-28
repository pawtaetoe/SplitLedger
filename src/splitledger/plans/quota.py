import calendar
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from splitledger.db.models import Budget, BudgetPlan, LedgerEntry, LedgerKind, User
from splitledger.handlers.log import money

# alert once at each of these shares of a bucket
THRESHOLDS = (80, 100)


# first local day of the period that contains today
def period_start(today: date, period: str) -> date:
    if period == "weekly":
        return today - timedelta(days=today.weekday())
    return today.replace(day=1)


# key that changes when a new period starts
def period_key(today: date, period: str) -> str:
    if period == "weekly":
        year, week, _ = today.isocalendar()
        return f"{year}-W{week:02d}"
    return today.strftime("%Y-%m")


# days left in the period including today
def days_left(today: date, period: str) -> int:
    if period == "weekly":
        return 7 - today.weekday()
    return calendar.monthrange(today.year, today.month)[1] - today.day + 1


# a monthly amount scaled to the period, weeks are a year of months over 52
def quota_cents(take_home_cents: int, pct: int, period: str) -> int:
    if period == "weekly":
        return take_home_cents * pct * 12 // (100 * 52)
    return take_home_cents * pct // 100


# where one bucket stands this period
@dataclass(frozen=True)
class BucketStatus:
    name: str
    quota_cents: int
    spent_cents: int
    saving: bool

    # money still free, negative when over
    def left_cents(self) -> int:
        return self.quota_cents - self.spent_cents

    # whole percent of the quota used
    def used_pct(self) -> int:
        return self.spent_cents * 100 // self.quota_cents if self.quota_cents else 0


# the user plan with this period's numbers
@dataclass(frozen=True)
class PlanReport:
    plan: BudgetPlan
    user: User
    today: date
    buckets: list[BucketStatus]

    # week or month for reply text
    def unit(self) -> str:
        return "week" if self.plan.period == "weekly" else "month"


# reads plans and spending to give quotas, lines after a log and alerts
class QuotaBook:
    # sessions for plan and ledger reads
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    # this period for every bucket, none without a plan or income
    async def report(self, user_id: uuid.UUID) -> PlanReport | None:
        async with self._sessions() as session:
            user = await session.get(User, user_id)
            query = select(BudgetPlan).where(BudgetPlan.user_id == user_id)
            plan = (await session.execute(query)).scalar_one_or_none()
            if user is None or plan is None or not user.take_home_cents:
                return None
            today = datetime.now(ZoneInfo(user.timezone)).date()
            spent = await self._spent(session, user, plan, today)
        saving = set(plan.buckets) - set(plan.category_map.values())
        buckets = [
            BucketStatus(
                name, quota_cents(user.take_home_cents, pct, plan.period),
                spent.get(name, 0), name in saving
            )
            for name, pct in plan.buckets.items()
        ]
        return PlanReport(plan, user, today, buckets)

    # the line to add after logging a category, plus an alert the first time it crosses
    async def after_save(self, user_id: uuid.UUID, category: str) -> list[str]:
        report = await self.report(user_id)
        if report is None:
            return []
        name = report.plan.category_map.get(category)
        bucket = next((b for b in report.buckets if b.name == name), None)
        if bucket is None or bucket.saving or not bucket.quota_cents:
            return []
        lines = [left_line(bucket, report)]
        crossed = max((t for t in THRESHOLDS if bucket.used_pct() >= t), default=0)
        if crossed and await self._first_alert(report, bucket, crossed):
            lines.append(alert_line(bucket, report, crossed))
        return lines

    # plain numbers for the assistant tool
    async def status(self, user_id: uuid.UUID) -> dict[str, Any]:
        report = await self.report(user_id)
        if report is None:
            return {"plan": None, "hint": "no plan yet, the user can say set up a plan"}
        currency = report.user.currency
        return {
            "plan": report.plan.framework,
            "period": report.unit(),
            "days_left": days_left(report.today, report.plan.period),
            "buckets": [
                {
                    "name": b.name,
                    "target" if b.saving else "quota": money(b.quota_cents, currency),
                    "spent": None if b.saving else money(b.spent_cents, currency),
                    "left": None if b.saving else money(b.left_cents(), currency)
                }
                for b in report.buckets
            ]
        }

    # spending this period per bucket in the user currency
    async def _spent(
        self, session: AsyncSession, user: User, plan: BudgetPlan, today: date
    ) -> dict[str, int]:
        start = datetime.combine(period_start(today, plan.period), time(), ZoneInfo(user.timezone))
        query = (
            select(LedgerEntry.category, func.sum(LedgerEntry.amount_cents))
            .where(
                LedgerEntry.user_id == user.id,
                LedgerEntry.kind == LedgerKind.EXPENSE,
                LedgerEntry.voided_at.is_(None),
                LedgerEntry.currency == user.currency,
                LedgerEntry.occurred_at >= start
            )
            .group_by(LedgerEntry.category)
        )
        totals: dict[str, int] = {}
        for category, cents in (await session.execute(query)).all():
            bucket = plan.category_map.get(category or "Other")
            if bucket is not None:
                totals[bucket] = totals.get(bucket, 0) + int(cents)
        return totals

    # record the threshold, true only the first time it is reached this period
    async def _first_alert(self, report: PlanReport, bucket: BucketStatus, crossed: int) -> bool:
        key = period_key(report.today, report.plan.period)
        async with self._sessions() as session:
            await session.execute(
                insert(Budget)
                .values(
                    user_id=report.user.id, category=bucket.name, month=key,
                    limit_cents=bucket.quota_cents, last_alert_pct=0
                )
                .on_conflict_do_nothing(constraint="uq_budget_month")
            )
            row = (
                await session.execute(
                    select(Budget).where(
                        Budget.user_id == report.user.id,
                        Budget.category == bucket.name,
                        Budget.month == key
                    ).with_for_update()
                )
            ).scalar_one()
            first = crossed > row.last_alert_pct
            if first:
                row.last_alert_pct = crossed
            await session.commit()
        return first


# what is left in a bucket this period
def left_line(bucket: BucketStatus, report: PlanReport) -> str:
    currency = report.user.currency
    left = bucket.left_cents()
    where = f"in {bucket.name} this {report.unit()}"
    if left < 0:
        return f"*{money(-left, currency)}* over {where}"
    return f"*{money(left, currency)}* left {where}"


# a short heads up at 80 percent and when it runs out
def alert_line(bucket: BucketStatus, report: PlanReport, crossed: int) -> str:
    days = days_left(report.today, report.plan.period)
    if crossed >= 100:
        return f"*Heads up:* you've used all of {bucket.name} this {report.unit()}."
    plural = "s" if days != 1 else ""
    return f"*Heads up:* {crossed}% of {bucket.name} used, {days} day{plural} to go."
