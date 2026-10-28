import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, get_args
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from splitledger.contracts.llm import LLM, Tool
from splitledger.contracts.messages import Inbound
from splitledger.contracts.transport import Transport
from splitledger.db.models import LedgerEntry, LedgerKind, User
from splitledger.handlers.log import money
from splitledger.llm.schemas import Category

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
DAYS = {"type": "string", "description": "local calendar day as YYYY-MM-DD"}
RANGE = {"start": DAYS, "end": {**DAYS, "description": "last local day, inclusive"}}
CATEGORY = {"type": "string", "enum": CATEGORIES}
Query = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


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
        self, sessions: async_sessionmaker[AsyncSession], user_id: uuid.UUID, zone: ZoneInfo
    ) -> None:
        self._sessions = sessions
        self._user_id = user_id
        self._zone = zone

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
            Tool("who_owes_me", "money friends still owe from shared bills",
                 guarded(self.who_owes_me))
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

    # open money owed to the user, by friend
    async def who_owes_me(self, args: dict[str, Any]) -> dict[str, Any]:
        query = (
            select(User.display_name, LedgerEntry.currency, func.sum(LedgerEntry.amount_cents))
            .join(User, LedgerEntry.counterparty_id == User.id, isouter=True)
            .where(
                LedgerEntry.user_id == self._user_id,
                LedgerEntry.kind == LedgerKind.RECEIVABLE,
                LedgerEntry.voided_at.is_(None)
            )
            .group_by(User.display_name, LedgerEntry.currency)
        )
        async with self._sessions() as session:
            rows = (await session.execute(query)).all()
        owed = [{"who": name or "a friend", "owes": money(int(cents), code)}
                for name, code, cents in rows]
        return {"owed": owed}

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
    # model for the tool loop and a session factory for the queries
    def __init__(self, llm: LLM, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._llm = llm
        self._sessions = sessions

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
        tools = SpendTools(self._sessions, user.id, zone).tools()
        try:
            result = await asyncio.wait_for(
                self._llm.run_tools(system, inbound.text or "", tools), ANSWER_TIMEOUT
            )
        except Exception as error:
            LOG.warning("spend assistant failed with %s", type(error).__name__)
            await transport.send_text(inbound.user_id, UNAVAILABLE)
            return
        await transport.send_text(inbound.user_id, result.text.strip() or UNAVAILABLE)
