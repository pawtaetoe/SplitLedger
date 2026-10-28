import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from splitledger.db import models
from splitledger.db.models import Bill, BillItem, BillStatus, LedgerEntry, LedgerKind, User
from splitledger.handlers.bill import even_share
from splitledger.plans.quota import PlanReport, QuotaBook
from splitledger.plans.upcoming import open_days, outlook
from splitledger.split.claims import preview_share
from splitledger.split.fx import RateSource, convert

# how far the dashboard looks for the next day a bill is due
NEXT_DAYS = 400
RECENT = 10
OPEN_BILLS = 10
# category, currency and cents
Spent = tuple[str, str, int]
# per category: currency, cents in it and cents in the main currency
Parts = dict[str, list[tuple[str, int, int]]]


# money one person owes or is owed, in one currency, with their account when they have one
@dataclass(frozen=True)
class Balance:
    who: str
    cents: int
    currency: str
    user: uuid.UUID | None = None
    wa: str | None = None


# an open shared bill and how far claiming has got
@dataclass(frozen=True)
class OpenBill:
    bill: Bill
    claimed: int
    items: int
    people: int
    # the viewer's share so far, nothing is owed until the payer closes the bill
    mine: int = 0
    # everyone else's shares so far
    others: int = 0
    payer_name: str = ""


# one upcoming bill with its next day
@dataclass(frozen=True)
class Coming:
    plan: models.PlannedSpend
    next_on: date


# everything the dashboard shows for one user
@dataclass(frozen=True)
class Dashboard:
    user: User
    owed_to_me: list[Balance]
    i_owe: list[Balance]
    paid: list[OpenBill]
    joined: list[OpenBill]
    month: list[tuple[str, int]]
    report: PlanReport | None
    coming: list[Coming]
    still_due: int
    recent: list[LedgerEntry]
    # this month in currencies with no rate today, category, currency and cents
    abroad: list[Spent] = field(default_factory=list)
    # each category's amounts by currency with their value in the main currency
    parts: Parts = field(default_factory=dict)

    # true when any category mixes in another currency at today's rate
    def converted(self) -> bool:
        main = self.user.currency
        return any(code != main for rows in self.parts.values() for code, _, _ in rows)

    # this month's spending in the main currency
    def month_total(self) -> int:
        return sum(cents for _, cents in self.month)

    # this month's spending in other currencies, one total each
    def other_totals(self) -> list[tuple[str, int]]:
        totals: dict[str, int] = {}
        for _, code, cents in self.abroad:
            totals[code] = totals.get(code, 0) + cents
        return sorted(totals.items())

    # one net amount per person and currency, above zero they owe you, below you owe them
    def people(self) -> list[Balance]:
        net: dict[tuple[str, str], int] = {}
        account: dict[tuple[str, str], Balance] = {}
        for balance, sign in [(b, 1) for b in self.owed_to_me] + [(b, -1) for b in self.i_owe]:
            key = (balance.who, balance.currency)
            net[key] = net.get(key, 0) + sign * balance.cents
            if balance.user is not None:
                account[key] = balance
        found = [
            Balance(
                who, cents, code, account[(who, code)].user if (who, code) in account else None,
                account[(who, code)].wa if (who, code) in account else None
            )
            for (who, code), cents in net.items() if cents
        ]
        return sorted(found, key=lambda balance: -abs(balance.cents))

    # your share so far on open bills someone else paid, by currency
    def pending(self) -> list[tuple[str, int]]:
        totals: dict[str, int] = {}
        for row in self.joined:
            totals[row.bill.currency] = totals.get(row.bill.currency, 0) + row.mine
        return [(code, cents) for code, cents in totals.items() if cents]

    # net in the main currency, above zero friends owe you more than you owe
    def net(self) -> int:
        return sum(b.cents for b in self.people() if b.currency == self.user.currency)


# one person's share of an open bill as it stands, zero when they are not on it
def share_now(
    bill: Bill, items: list[BillItem], claims: list[models.ItemClaim],
    me: models.Participant | None
) -> int:
    if me is None:
        return 0
    if bill.even_ways:
        each, covers = even_share(bill.total, bill.even_ways)
        return covers if me.user_id == bill.payer_id else each
    return preview_share(bill, items, claims, me.id)


# spending in every currency turned into the main one at today's rate, with what went in
def combine(
    spent: list[Spent], rates: dict[str, float]
) -> tuple[list[tuple[str, int]], Parts, list[Spent]]:
    totals: dict[str, int] = {}
    parts: Parts = {}
    abroad: list[Spent] = []
    for name, code, cents in spent:
        value = convert(cents, code, rates)
        if value is None:
            abroad.append((name, code, cents))
            continue
        totals[name] = totals.get(name, 0) + value
        parts.setdefault(name, []).append((code, cents, value))
    month = sorted(totals.items(), key=lambda pair: -pair[1])
    return month, parts, abroad


# reads one user's money across expenses, bills, plans and upcoming bills
class Overview:
    # sessions, the quota book for plan parts and exchange rates for other currencies
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], book: QuotaBook,
        rates: RateSource | None = None
    ) -> None:
        self._sessions = sessions
        self._book = book
        self._rates = rates

    # the whole dashboard, none for an unknown user
    async def build(self, user_id: uuid.UUID) -> Dashboard | None:
        async with self._sessions() as session:
            user = await session.get(User, user_id)
            if user is None:
                return None
            zone = ZoneInfo(user.timezone)
            today = datetime.now(zone).date()
            owed = await self._balances(session, user_id, LedgerKind.RECEIVABLE)
            owe = await self._balances(session, user_id, LedgerKind.PAYABLE)
            paid = await self._open_bills(session, user_id, Bill.payer_id == user_id)
            mine = select(models.Participant.bill_id).where(models.Participant.user_id == user_id)
            joined = await self._open_bills(
                session, user_id, Bill.payer_id != user_id, Bill.id.in_(mine)
            )
            spent = await self._month(session, user_id, today, zone)
            plans = list((await session.execute(
                select(models.PlannedSpend).where(
                    models.PlannedSpend.user_id == user_id, models.PlannedSpend.voided_at.is_(None)
                )
            )).scalars())
            recent = list((await session.execute(
                select(LedgerEntry)
                .where(
                    LedgerEntry.user_id == user_id, LedgerEntry.kind == LedgerKind.EXPENSE,
                    LedgerEntry.voided_at.is_(None)
                )
                .order_by(LedgerEntry.occurred_at.desc())
                .limit(RECENT)
            )).scalars())
        coming: list[Coming] = []
        for plan in plans:
            days = open_days(plan, today, today + timedelta(days=NEXT_DAYS))
            if days:
                coming.append(Coming(plan, days[0]))
        coming.sort(key=lambda item: item.next_on)
        due = outlook([p for p in plans if p.currency == user.currency], today).due_total()
        report = await self._book.report(user_id)
        rates = await self._rates.to(user.currency) if self._rates else {user.currency: 1.0}
        month, parts, abroad = combine(spent, rates)
        return Dashboard(
            user, owed, owe, paid, joined, month, report, coming, due, recent, abroad, parts
        )

    # owed or owing totals by the other person less what was paid, guests named from the note
    async def _balances(
        self, session: AsyncSession, user_id: uuid.UUID, kind: LedgerKind
    ) -> list[Balance]:
        who = func.coalesce(User.display_name, LedgerEntry.note, "a friend")
        settled = LedgerEntry.kind == LedgerKind.SETTLEMENT
        cents = func.sum(
            case((settled, -func.abs(LedgerEntry.amount_cents)), else_=LedgerEntry.amount_cents)
        )
        side = LedgerEntry.amount_cents > 0 if kind == LedgerKind.RECEIVABLE else (
            LedgerEntry.amount_cents < 0
        )
        query = (
            select(who, LedgerEntry.currency, cents, LedgerEntry.counterparty_id, User.wa_user_id)
            .join(User, LedgerEntry.counterparty_id == User.id, isouter=True)
            .where(
                LedgerEntry.user_id == user_id, LedgerEntry.voided_at.is_(None),
                or_(LedgerEntry.kind == kind, and_(settled, side))
            )
            .group_by(who, LedgerEntry.currency, LedgerEntry.counterparty_id, User.wa_user_id)
            .having(cents != 0)
            .order_by(cents.desc())
        )
        rows = (await session.execute(query)).all()
        return [
            Balance(str(name), int(cents), currency, user, wa)
            for name, currency, cents, user, wa in rows
        ]

    # open bills matching the filters, newest first, with claim progress
    async def _open_bills(
        self, session: AsyncSession, user_id: uuid.UUID, *where: Any
    ) -> list[OpenBill]:
        bills = list((await session.execute(
            select(Bill)
            .where(Bill.status == BillStatus.OPEN, *where)
            .order_by(Bill.created_at.desc())
            .limit(OPEN_BILLS)
        )).scalars())
        found: list[OpenBill] = []
        for bill in bills:
            items = list((await session.execute(
                select(BillItem).where(BillItem.bill_id == bill.id)
            )).scalars())
            claims = list((await session.execute(
                select(models.ItemClaim)
                .where(models.ItemClaim.bill_item_id.in_([item.id for item in items]))
            )).scalars())
            people = list((await session.execute(
                select(models.Participant).where(models.Participant.bill_id == bill.id)
            )).scalars())
            me = next((p for p in people if p.user_id == user_id), None)
            claimed = len({claim.bill_item_id for claim in claims})
            others = sum(share_now(bill, items, claims, p) for p in people if p is not me)
            payer = next((p.guest_name for p in people if p.user_id == bill.payer_id), "")
            found.append(OpenBill(
                bill, claimed, len(items), len(people), share_now(bill, items, claims, me), others,
                payer
            ))
        return found

    # this month's spending by category and currency, biggest first
    async def _month(
        self, session: AsyncSession, user_id: uuid.UUID, today: date, zone: ZoneInfo
    ) -> list[Spent]:
        start = datetime.combine(today.replace(day=1), time(), zone)
        total = func.sum(LedgerEntry.amount_cents)
        rows = (await session.execute(
            select(LedgerEntry.category, LedgerEntry.currency, total)
            .where(
                LedgerEntry.user_id == user_id, LedgerEntry.kind == LedgerKind.EXPENSE,
                LedgerEntry.voided_at.is_(None), LedgerEntry.occurred_at >= start
            )
            .group_by(LedgerEntry.category, LedgerEntry.currency)
            .order_by(total.desc())
        )).all()
        return [(category or "Other", code, int(cents)) for category, code, cents in rows]
