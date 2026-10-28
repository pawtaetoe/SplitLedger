import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, get_args
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from splitledger.contracts.llm import LLM, Tool
from splitledger.contracts.messages import Inbound
from splitledger.contracts.transport import Transport
from splitledger.db.models import LedgerEntry, LedgerKind, PlannedSpend, User
from splitledger.handlers.log import money
from splitledger.llm.schemas import Category
from splitledger.plans import afford
from splitledger.plans.afford import Ask, Means, Rule, Spending, assess, to_cents
from splitledger.plans.quota import QuotaBook
from splitledger.plans.upcoming import EVERY, open_days, outlook
from splitledger.split.overview import Overview

LOG = logging.getLogger(__name__)
PROMPT = (Path(__file__).parents[1] / "llm" / "prompts" / "assistant.md").read_text(
    encoding="utf-8"
)
CATEGORIES = list(get_args(Category))
# seconds for the whole tool loop before we give up
ANSWER_TIMEOUT = 25.0
# longest range a question may cover
MAX_DAYS = 366
LIST_LIMIT = 20
CHECKING = "Checking your spending…"
NOTHING_YET = "You haven't logged anything yet. Try something like coffee 4.50 first"
UNAVAILABLE = "I couldn't work that out just now. Please try again in a minute"
DISCLAIMER = "_A rule of thumb, not financial advice._"
ALL_SQUARE = "*You're all square*\nNobody owes anybody right now."
# days ahead to look for the next time a plan is due
NEXT_DAYS = 400
DAYS = {"type": "string", "description": "local calendar day as YYYY-MM-DD"}
RANGE = {"start": DAYS, "end": {**DAYS, "description": "last local day, inclusive"}}
CATEGORY = {"type": "string", "enum": CATEGORIES}
Query = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
AMOUNT = {"type": "number", "description": "plain amount in their currency, not cents"}
AFFORD = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(afford.KINDS)},
        "price": AMOUNT,
        "down_payment": AMOUNT,
        "down_payment_pct": {"type": "number", "description": "down payment as a percent"},
        "monthly_payment": AMOUNT,
        "running_costs": {**AMOUNT, "description": "monthly insurance, fuel, parking"},
        "term_months": {"type": "integer"},
        "other_monthly_debt": AMOUNT,
        "property_type": {"type": "string", "enum": list(afford.PROPERTY_TYPES)},
        "omv": {**AMOUNT, "description": "Singapore open market value of a car"},
        "category": CATEGORY,
        "take_home": {**AMOUNT, "description": "monthly take home pay, only if they said it"},
        "gross_income": {**AMOUNT, "description": "monthly pay before tax, only if they said it"}
    },
    "required": ["kind"]
}
UPCOMING = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "description": "short name like rent or mom's present"},
        "amount": AMOUNT,
        "every": {"type": "string", "enum": list(EVERY)},
        "on": {**DAYS, "description": "the next local day it is due"},
        "category": CATEGORY,
        "remind": {"type": "boolean", "description": "true only if they asked for a reminder"}
    },
    "required": ["label", "amount", "every", "on"]
}
LABEL = {"type": "object", "properties": {"label": {"type": "string"}}, "required": ["label"]}


# utc start and exclusive end for local days from start to end, error on bad input
def day_range(args: dict[str, Any], zone: ZoneInfo) -> tuple[datetime, datetime]:
    start = date.fromisoformat(str(args.get("start", "")))
    end = date.fromisoformat(str(args.get("end", "")))
    if end < start or (end - start).days >= MAX_DAYS:
        raise ValueError("end must be on or after start and within a year")
    first = datetime.combine(start, time(), zone)
    after = datetime.combine(end + timedelta(days=1), time(), zone)
    return first, after


# category argument checked against the fixed list, none when not given
def category_arg(args: dict[str, Any]) -> str | None:
    category = args.get("category")
    if category is None:
        return None
    if category not in CATEGORIES:
        raise ValueError(f"category must be one of {', '.join(CATEGORIES)}")
    return str(category)


# the first day of the month n months before the given first of a month
def months_back(first: date, n: int) -> date:
    index = first.year * 12 + first.month - 1 - n
    return date(index // 12, index % 12 + 1, 1)


# full months of history before this month, at most three, from the first expense day
def full_months(first_spend: date | None, month_start: date) -> int:
    if first_spend is None:
        return 0
    gap = (month_start.year * 12 + month_start.month) - (first_spend.year * 12 + first_spend.month)
    return max(0, min(3, gap - (0 if first_spend.day == 1 else 1)))


# bad arguments go back to the model as an error it can fix
def guarded(query: Query) -> Query:
    async def run(args: dict[str, Any]) -> dict[str, Any]:
        try:
            return await query(args)
        except (TypeError, ValueError) as error:
            return {"error": str(error)}
    return run


# read only spending queries for one user, each one a tool the model may call
class SpendTools:
    # every query is scoped to this user and read in their timezone
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], user_id: uuid.UUID, zone: ZoneInfo,
        book: QuotaBook, rules: list[Rule]
    ) -> None:
        self._sessions = sessions
        self._user_id = user_id
        self._zone = zone
        self._book = book
        self._rules = rules
        # plans saved during this answer and whether each has a reminder, for the buttons
        self.added: list[tuple[uuid.UUID, bool]] = []

    # the tools with their argument schemas
    def tools(self) -> list[Tool]:
        ranged = {"type": "object", "properties": RANGE, "required": ["start", "end"]}
        with_category = {**ranged, "properties": {**RANGE, "category": CATEGORY}}
        with_count = {
            **ranged,
            "properties": {**RANGE, "n": {"type": "integer", "minimum": 1, "maximum": 10}}
        }
        return [
            Tool("sum_spend", "total spent between two days, optionally one category",
                 guarded(self.sum_spend), with_category),
            Tool("top_expenses", "biggest single expenses between two days",
                 guarded(self.top_expenses), with_count),
            Tool("list_expenses", "expenses between two days, newest first",
                 guarded(self.list_expenses), with_category),
            Tool("balances", "who owes the user, whom the user owes, and shares on open bills",
                 guarded(self.balances)),
            Tool("plan_status", "budget plan buckets with quota, spent and left this period",
                 guarded(self.plan_status)),
            Tool("can_afford", "check a purchase, car or home against named affordability rules",
                 guarded(self.can_afford), AFFORD),
            Tool("add_upcoming", "save a bill or purchase they expect, once or on repeat",
                 guarded(self.add_upcoming), UPCOMING),
            Tool("list_upcoming", "bills and purchases they expect, with what is due this month",
                 guarded(self.list_upcoming)),
            Tool("remove_upcoming", "stop expecting a bill or purchase, by its name",
                 guarded(self.remove_upcoming), LABEL)
        ]

    # total per currency
    async def sum_spend(self, args: dict[str, Any]) -> dict[str, Any]:
        query = (
            select(LedgerEntry.currency, func.sum(LedgerEntry.amount_cents), func.count())
            .where(*self._filters(args))
            .group_by(LedgerEntry.currency)
        )
        async with self._sessions() as session:
            rows = (await session.execute(query)).all()
        totals = [{"total": money(int(cents), code), "count": count} for code, cents, count in rows]
        return {"totals": totals}

    # largest expenses first
    async def top_expenses(self, args: dict[str, Any]) -> dict[str, Any]:
        n = int(args.get("n", 3))
        if not 1 <= n <= 10:
            raise ValueError("n must be between 1 and 10")
        order = LedgerEntry.amount_cents.desc()
        return {"expenses": await self._rows(args, order, n)}

    # newest expenses first, capped
    async def list_expenses(self, args: dict[str, Any]) -> dict[str, Any]:
        rows = await self._rows(args, LedgerEntry.occurred_at.desc(), LIST_LIMIT + 1)
        return {"expenses": rows[:LIST_LIMIT], "more": len(rows) > LIST_LIMIT}

    # who owes the user and whom the user owes, plus shares on bills still open
    async def balances(self, args: dict[str, Any]) -> dict[str, Any]:
        board = await Overview(self._sessions, self._book).build(self._user_id)
        if board is None:
            return {"error": "unknown user"}
        people = board.people()
        return {
            "they_owe_you": [
                {"who": b.who, "amount": money(b.cents, b.currency)} for b in people if b.cents > 0
            ],
            "you_owe": [
                {"who": b.who, "amount": money(-b.cents, b.currency)} for b in people if b.cents < 0
            ],
            "open_bills_you_are_in": [
                {"bill": row.bill.vendor or row.bill.code, "paid_by": row.payer_name,
                 "your_share_so_far": money(row.mine, row.bill.currency)}
                for row in board.joined
            ],
            "open_bills_you_paid": [
                {"bill": row.bill.vendor or row.bill.code,
                 "friends_share_so_far": money(row.others, row.bill.currency)}
                for row in board.paid
            ],
            "note": "open bill shares are not owed until the bill closes"
        }

    # the plan for this period from the quota book
    async def plan_status(self, args: dict[str, Any]) -> dict[str, Any]:
        return await self._book.status(self._user_id)

    # rules check the numbers, pay they mention is remembered for next time
    async def can_afford(self, args: dict[str, Any]) -> dict[str, Any]:
        ask = Ask.from_args(args)
        take_home = to_cents(args.get("take_home"))
        gross = to_cents(args.get("gross_income"))
        async with self._sessions() as session:
            user = await session.get(User, self._user_id)
            if user is None:
                return {"error": "unknown user"}
            if take_home or gross:
                values = {"take_home_cents": take_home} if take_home else {}
                values |= {"gross_income_cents": gross} if gross else {}
                await session.execute(update(User).where(User.id == user.id).values(**values))
                await session.commit()
            currency = user.currency
        today = datetime.now(self._zone).date()
        means = Means(
            currency, take_home or user.take_home_cents,
            gross or user.gross_income_cents, await self._book.report(self._user_id),
            await self._spending(ask.category, currency) if ask.kind == "purchase" else None,
            outlook(await self._planned(currency), today)
        )
        return assess(ask, means, self._rules)

    # save a bill or purchase they expect, a one off can't be in the past
    async def add_upcoming(self, args: dict[str, Any]) -> dict[str, Any]:
        every = str(args.get("every", ""))
        if every not in EVERY:
            raise ValueError(f"every must be one of {', '.join(EVERY)}")
        label = str(args.get("label", "")).strip()
        amount = to_cents(args.get("amount"))
        if not label or not amount:
            raise ValueError("label and an amount above 0 are needed")
        on = date.fromisoformat(str(args.get("on", "")))
        if every == "once" and on < datetime.now(self._zone).date():
            raise ValueError("a one off must be due today or later")
        async with self._sessions() as session:
            user = await session.get(User, self._user_id)
            if user is None:
                return {"error": "unknown user"}
            plan = PlannedSpend(
                id=uuid.uuid4(), user_id=user.id, label=label, amount_cents=amount,
                currency=user.currency, category=category_arg(args), every=every, due_on=on,
                remind=args.get("remind") is True
            )
            session.add(plan)
            await session.commit()
        self.added.append((plan.id, plan.remind))
        saved = money(amount, plan.currency)
        return {"saved": label, "amount": saved, "every": every, "on": on.isoformat()}

    # every active plan with its next day, plus totals for this month and a usual month
    async def list_upcoming(self, args: dict[str, Any]) -> dict[str, Any]:
        currency = await self._currency()
        today = datetime.now(self._zone).date()
        plans = await self._planned(currency)
        ahead = outlook(plans, today)
        items = []
        for plan in plans:
            days = open_days(plan, today, today + timedelta(days=NEXT_DAYS))
            if days:
                items.append({
                    "what": plan.label, "amount": money(plan.amount_cents, currency),
                    "every": plan.every, "next": days[0].isoformat()
                })
        return {
            "upcoming": sorted(items, key=lambda item: item["next"]),
            "still_to_pay_this_month": money(ahead.due_total(), currency),
            "repeating_per_month": money(ahead.monthly, currency)
        }

    # void every active plan whose name matches, never delete
    async def remove_upcoming(self, args: dict[str, Any]) -> dict[str, Any]:
        label = str(args.get("label", "")).strip()
        if not label:
            raise ValueError("label is required")
        query = (
            update(PlannedSpend)
            .where(
                PlannedSpend.user_id == self._user_id,
                PlannedSpend.voided_at.is_(None),
                PlannedSpend.label.ilike(f"%{label}%")
            )
            .values(voided_at=func.now())
            .returning(PlannedSpend.label)
        )
        async with self._sessions() as session:
            removed = list((await session.execute(query)).scalars())
            await session.commit()
        if not removed:
            names = [plan.label for plan in await self._planned(await self._currency())]
            return {"error": f"no upcoming bill matches {label}", "upcoming": names}
        return {"removed": removed}

    # the user's main currency
    async def _currency(self) -> str:
        async with self._sessions() as session:
            user = await session.get(User, self._user_id)
        if user is None:
            raise ValueError("unknown user")
        return user.currency

    # active plans in one currency
    async def _planned(self, currency: str) -> list[PlannedSpend]:
        query = select(PlannedSpend).where(
            PlannedSpend.user_id == self._user_id,
            PlannedSpend.voided_at.is_(None),
            PlannedSpend.currency == currency
        )
        async with self._sessions() as session:
            return list((await session.execute(query)).scalars())

    # this month so far and a usual month from up to three full months of history
    async def _spending(self, category: str | None, currency: str) -> Spending:
        month_start = datetime.now(self._zone).date().replace(day=1)
        async with self._sessions() as session:
            first = (await session.execute(
                select(func.min(LedgerEntry.occurred_at)).where(*self._mine(currency))
            )).scalar_one_or_none()
        first_day = first.astimezone(self._zone).date() if first else None
        months = full_months(first_day, month_start)
        average_from = months_back(month_start, months)
        this_month = await self._total(currency, month_start, None)
        category_month = await self._total(currency, month_start, None, category) if category else 0
        window = await self._total(currency, average_from, month_start) if months else 0
        category_window = (
            await self._total(currency, average_from, month_start, category)
            if months and category else 0
        )
        return Spending(
            this_month, category_month, window // months if months else 0,
            category_window // months if months else 0, months
        )

    # expenses of this user in one currency that still count
    def _mine(self, currency: str) -> list[Any]:
        return [
            LedgerEntry.user_id == self._user_id,
            LedgerEntry.kind == LedgerKind.EXPENSE,
            LedgerEntry.voided_at.is_(None),
            LedgerEntry.currency == currency
        ]

    # total spent from one local day to before another, optionally one category
    async def _total(
        self, currency: str, start: date, end: date | None, category: str | None = None
    ) -> int:
        filters = [
            *self._mine(currency),
            LedgerEntry.occurred_at >= datetime.combine(start, time(), self._zone)
        ]
        if end is not None:
            filters.append(LedgerEntry.occurred_at < datetime.combine(end, time(), self._zone))
        if category is not None:
            filters.append(LedgerEntry.category == category)
        query = select(func.coalesce(func.sum(LedgerEntry.amount_cents), 0)).where(*filters)
        async with self._sessions() as session:
            return int((await session.execute(query)).scalar_one())

    # expense filters for this user, the range and an optional category
    def _filters(self, args: dict[str, Any]) -> list[Any]:
        first, after = day_range(args, self._zone)
        filters = [
            LedgerEntry.user_id == self._user_id,
            LedgerEntry.kind == LedgerKind.EXPENSE,
            LedgerEntry.voided_at.is_(None),
            LedgerEntry.occurred_at >= first,
            LedgerEntry.occurred_at < after
        ]
        category = category_arg(args)
        if category is not None:
            filters.append(LedgerEntry.category == category)
        return filters

    # expense rows as plain values the model can read
    async def _rows(self, args: dict[str, Any], order: Any, limit: int) -> list[dict[str, Any]]:
        query = (
            select(LedgerEntry)
            .where(*self._filters(args))
            .order_by(order)
            .limit(limit)
        )
        async with self._sessions() as session:
            entries = (await session.execute(query)).scalars().all()
        return [
            {
                "day": entry.occurred_at.astimezone(self._zone).date().isoformat(),
                "amount": money(entry.amount_cents, entry.currency),
                "category": entry.category,
                "what": entry.vendor or entry.note or ""
            }
            for entry in entries
        ]


# answers spending questions with the model calling read only tools
class SpendAssistant:
    # model for the tool loop, sessions, the quota book and the affordability rules in use
    def __init__(
        self, llm: LLM, sessions: async_sessionmaker[AsyncSession], book: QuotaBook,
        rules: list[Rule]
    ) -> None:
        self._llm = llm
        self._sessions = sessions
        self._book = book
        self._rules = rules

    # who owes whom straight from the ledger, with the dashboard one tap away
    async def owed(self, inbound: Inbound, transport: Transport) -> None:
        async with self._sessions() as session:
            query = select(User.id).where(User.wa_user_id == inbound.user_id)
            user_id = (await session.execute(query)).scalar_one_or_none()
        text = await owed_text(self._sessions, self._book, user_id) if user_id else ALL_SQUARE
        await transport.send_buttons(inbound.user_id, text, [("menu:dash", "My dashboard")])

    # say we are checking, run the tool loop, reply with its answer or a fallback
    async def answer(self, inbound: Inbound, transport: Transport) -> None:
        async with self._sessions() as session:
            query = select(User).where(User.wa_user_id == inbound.user_id)
            user = (await session.execute(query)).scalar_one_or_none()
        if user is None:
            await transport.send_text(inbound.user_id, NOTHING_YET)
            return
        await transport.send_text(inbound.user_id, CHECKING)
        zone = ZoneInfo(user.timezone)
        today = datetime.now(zone).date()
        system = (
            PROMPT.replace("{today}", f"{today.isoformat()}, a {today:%A}")
            .replace("{timezone}", user.timezone)
            .replace("{currency}", user.currency)
        )
        spend = SpendTools(self._sessions, user.id, zone, self._book, self._rules)
        try:
            result = await asyncio.wait_for(
                self._llm.run_tools(system, inbound.text or "", spend.tools()), ANSWER_TIMEOUT
            )
        except Exception as error:
            LOG.warning("spend assistant failed with %s", type(error).__name__)
            await transport.send_text(inbound.user_id, UNAVAILABLE)
            return
        reply = result.text.strip() or UNAVAILABLE
        # every affordability answer carries the same caveat, whatever the model wrote
        if "can_afford" in result.calls and DISCLAIMER not in reply:
            reply = f"{reply}\n\n{DISCLAIMER}"
        if spend.added:
            keys = ",".join(str(key) for key, _ in spend.added)
            buttons = [(f"bills:undo:{keys}", "Undo")]
            if not all(remind for _, remind in spend.added):
                buttons.append((f"bills:remind:{keys}", "Remind me"))
            await transport.send_buttons(inbound.user_id, reply, buttons)
            return
        await transport.send_text(inbound.user_id, reply)


# a plain list of who owes whom, for the button under a you owe message, no model needed
async def owed_text(
    sessions: async_sessionmaker[AsyncSession], book: QuotaBook, user_id: uuid.UUID
) -> str:
    board = await Overview(sessions, book).build(user_id)
    if board is None:
        return ALL_SQUARE
    people = board.people()
    owe = [f"- {b.who} *{money(-b.cents, b.currency)}*" for b in people if b.cents < 0]
    owed = [f"- {b.who} *{money(b.cents, b.currency)}*" for b in people if b.cents > 0]
    waiting = [
        f"- {row.payer_name or 'whoever paid'} for {row.bill.vendor or 'a bill'}: "
        f"{money(row.mine, row.bill.currency)} so far"
        for row in board.joined if row.mine
    ]
    parts: list[str] = []
    if owe:
        parts.append("*You owe*\n" + "\n".join(owe))
    if owed:
        parts.append("*Owed to you*\n" + "\n".join(owed))
    if waiting:
        parts.append("*Open bills, not owed yet*\n" + "\n".join(waiting))
    return "\n\n".join(parts) if parts else ALL_SQUARE
