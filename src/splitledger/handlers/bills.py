import logging
import uuid
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from splitledger.contracts.messages import Inbound
from splitledger.contracts.transport import Transport
from splitledger.db.models import PlannedSpend, User
from splitledger.handlers.log import ExpenseLogger, money
from splitledger.llm.schemas import ExpenseDraft
from splitledger.plans.upcoming import open_days, outlook

LOG = logging.getLogger(__name__)
# days ahead to look for the next time a bill is due
NEXT_DAYS = 400
EVERY_TEXT = {"once": "once", "week": "every week", "month": "every month", "year": "every year"}
NONE_YET = "*No upcoming bills yet*\nType one like _rent 1800 every month on the 1st_."
ADD_HINT = "To add one, type it like _phone 48 every month on the 5th_."
UNDONE = "Removed *{}* from your upcoming bills."
ALREADY = "That was already done."
REMIND_ON = "*Reminders on*\nI'll message you the day before it's due."
REMIND_OFF = "*Reminders off*\nIt stays in your upcoming bills."
MANAGE = "Tap a bill to remove it or change its reminder."
GONE = "*That bill is removed already*"
# bills offered as choices under the list, a whatsapp list holds ten
MAX_CHOICES = 10
NOT_YET = "*OK, still to pay*\nI'll keep it in what's due this month."


# day and short month like 3 Dec
def short_day(day: date) -> str:
    return f"{day.day} {day:%b}"


# the reminder text for one due day
def reminder_text(plan: PlannedSpend, day: date, today: date) -> str:
    when = "today" if day == today else "tomorrow"
    amount = money(plan.amount_cents, plan.currency)
    return f"*Reminder: {plan.label} {amount}*\nDue {when}, {day:%a} {short_day(day)}."


# ids from a button, empty when any is malformed
def parse_ids(keys: str) -> list[uuid.UUID]:
    try:
        return [uuid.UUID(key) for key in keys.split(",")]
    except ValueError:
        return []


# bills the user expects: shows them, reminds before they are due and marks them paid
class UpcomingBills:
    # sessions for the plans and the logger that saves a paid bill as an expense
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], logger: ExpenseLogger, remind_hour: int
    ) -> None:
        self._sessions = sessions
        self._logger = logger
        self._remind_hour = remind_hour

    # every active bill with its next day and what is still to pay this month
    async def show(self, inbound: Inbound, transport: Transport) -> None:
        async with self._sessions() as session:
            query = select(User).where(User.wa_user_id == inbound.user_id)
            user = (await session.execute(query)).scalar_one_or_none()
            active = select(PlannedSpend).where(
                PlannedSpend.user_id == (user.id if user else None),
                PlannedSpend.voided_at.is_(None)
            )
            plans = list((await session.execute(active)).scalars())
        if user is None or not plans:
            await transport.send_text(inbound.user_id, NONE_YET)
            return
        today = datetime.now(ZoneInfo(user.timezone)).date()
        lines: list[tuple[date, str, PlannedSpend]] = []
        for plan in plans:
            days = open_days(plan, today, today + timedelta(days=NEXT_DAYS))
            if days:
                bell = " · reminder on" if plan.remind else ""
                amount = money(plan.amount_cents, plan.currency)
                lines.append((days[0], (
                    f"- {plan.label} *{amount}* · {EVERY_TEXT[plan.every]}"
                    f" · next {short_day(days[0])}{bell}"
                ), plan))
        if not lines:
            await transport.send_text(inbound.user_id, NONE_YET)
            return
        lines.sort(key=lambda line: line[0])
        due = money(outlook(plans, today).due_total(), user.currency)
        body = "\n".join(text for _, text, _ in lines)
        buttons = [(f"bills:pick:{plan.id}", plan.label) for _, _, plan in lines[:MAX_CHOICES]]
        text = f"*Upcoming bills*\n{body}\n\nStill to pay this month: *{due}*"
        await transport.send_buttons(inbound.user_id, f"{text}\n{MANAGE}\n{ADD_HINT}", buttons)

    # bills buttons: pick, undo, remind, quiet, paid and not yet
    async def on_button(self, inbound: Inbound, button_id: str, transport: Transport) -> None:
        action, _, rest = button_id.removeprefix("bills:").partition(":")
        if action == "paid":
            await self._paid(inbound, rest, transport)
            return
        if action == "pick":
            await self._options(inbound, rest, transport)
            return
        if action not in ("undo", "remind", "quiet"):
            await transport.send_text(inbound.user_id, NOT_YET)
            return
        values = {"voided_at": func.now()} if action == "undo" else {"remind": action == "remind"}
        labels = await self._update(inbound, parse_ids(rest), values)
        if not labels:
            await transport.send_text(inbound.user_id, ALREADY)
            return
        done = {"undo": UNDONE.format(", ".join(labels)), "remind": REMIND_ON}
        await transport.send_text(inbound.user_id, done.get(action, REMIND_OFF))

    # one of the user's bills with remove and a reminder switch
    async def _options(self, inbound: Inbound, key: str, transport: Transport) -> None:
        query = (
            select(PlannedSpend)
            .join(User, PlannedSpend.user_id == User.id)
            .where(
                PlannedSpend.id.in_(parse_ids(key)),
                User.wa_user_id == inbound.user_id,
                PlannedSpend.voided_at.is_(None)
            )
        )
        async with self._sessions() as session:
            plan = (await session.execute(query)).scalars().first()
        if plan is None:
            await transport.send_text(inbound.user_id, GONE)
            return
        amount = money(plan.amount_cents, plan.currency)
        bell = "Reminder on" if plan.remind else "No reminder"
        switch = (f"bills:quiet:{plan.id}", "Reminders off") if plan.remind else (
            f"bills:remind:{plan.id}", "Remind me"
        )
        await transport.send_buttons(
            inbound.user_id,
            f"*{plan.label} {amount}*\n{EVERY_TEXT[plan.every].capitalize()} · {bell}",
            [(f"bills:undo:{plan.id}", "Remove"), switch]
        )

    # mark one due day paid once, then save it as an expense with the usual undo
    async def _paid(self, inbound: Inbound, rest: str, transport: Transport) -> None:
        key, _, day_text = rest.partition(":")
        ids = parse_ids(key)
        try:
            day = date.fromisoformat(day_text)
        except ValueError:
            ids = []
            day = date.min
        unpaid = PlannedSpend.paid_through.is_(None) | (PlannedSpend.paid_through < day)
        changed = await self._update(inbound, ids, {"paid_through": day}, unpaid)
        async with self._sessions() as session:
            plan = await session.get(PlannedSpend, ids[0]) if changed else None
        if plan is None:
            await transport.send_text(inbound.user_id, ALREADY)
            return
        draft = ExpenseDraft.model_validate({
            "amount_cents": plan.amount_cents, "currency": plan.currency,
            "category": plan.category or "Other", "note": plan.label
        })
        await self._logger.log(inbound, transport, draft)

    # update the user's own active bills, returning the labels changed
    async def _update(
        self, inbound: Inbound, ids: list[uuid.UUID], values: dict[str, Any], *extra: Any
    ) -> list[str]:
        owner = select(User.id).where(User.wa_user_id == inbound.user_id).scalar_subquery()
        query = (
            update(PlannedSpend)
            .where(
                PlannedSpend.id.in_(ids),
                PlannedSpend.user_id == owner,
                PlannedSpend.voided_at.is_(None),
                *extra
            )
            .values(**values)
            .returning(PlannedSpend.label)
        )
        async with self._sessions() as session:
            labels = list((await session.execute(query)).scalars())
            await session.commit()
        return labels

    # send one reminder per due day for bills due today or tomorrow, from the remind hour
    async def send_due(self, transport: Transport, now: datetime) -> int:
        query = (
            select(PlannedSpend, User)
            .join(User, PlannedSpend.user_id == User.id)
            .where(PlannedSpend.remind.is_(True), PlannedSpend.voided_at.is_(None))
        )
        async with self._sessions() as session:
            rows = (await session.execute(query)).all()
        sent = 0
        for plan, user in rows:
            local = now.astimezone(ZoneInfo(user.timezone))
            if local.hour < self._remind_hour:
                continue
            today = local.date()
            days = open_days(plan, today, today + timedelta(days=2))
            if not days or days[0] == plan.reminded_for:
                continue
            paid = f"bills:paid:{plan.id}:{days[0]}"
            buttons = [(paid, "Paid it"), (f"bills:later:{plan.id}", "Not yet")]
            try:
                await transport.send_buttons(
                    user.wa_user_id, reminder_text(plan, days[0], today), buttons
                )
            except Exception as error:
                LOG.warning("reminder failed with %s", type(error).__name__)
                continue
            async with self._sessions() as session:
                await session.execute(
                    update(PlannedSpend)
                    .where(PlannedSpend.id == plan.id)
                    .values(reminded_for=days[0])
                )
                await session.commit()
            sent += 1
        return sent
