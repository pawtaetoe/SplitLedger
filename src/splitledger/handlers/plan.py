import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from splitledger.contracts.messages import Inbound
from splitledger.contracts.transport import Transport
from splitledger.db.models import BudgetPlan, User
from splitledger.handlers.log import ensure_user, money
from splitledger.llm.schemas import Locale
from splitledger.plans.frameworks import PRESETS, SAVE_CHOICES, Framework, pay_yourself_first
from splitledger.plans.quota import QuotaBook, quota_cents

WELCOME = (
    "*Hi! I keep track of what you spend.*\n"
    "Type what you bought, like _coffee 4.50_.\n"
    "Or send a receipt photo or a voice note."
)
PICK = "*Pick a plan*\nYou can change it any time."
ASK_INCOME = "*What do you take home a month?*\nAfter tax. Just the number, like _4500_."
ASK_AGAIN = "*Just the number please*\nLike _4500_."
CANCEL = ("plan:cancel", "Cancel")
# an amount like 4500, 4,500.50 or 4.5k with an optional currency in front
INCOME = re.compile(r"^\s*(?:[a-z]{3}\s*|[^\d\s]{1,2})?(\d[\d,]*(?:\.\d+)?)\s*(k)?\s*$", re.I)
MAX_INCOME_CENTS = 1_000_000_000


# monthly income in cents from a reply, none when it is not a plain amount
def parse_income(text: str) -> int | None:
    match = INCOME.match(text)
    if not match:
        return None
    try:
        amount = Decimal(match.group(1).replace(",", ""))
    except InvalidOperation:
        return None
    cents = int(amount * (1000 if match.group(2) else 1) * 100)
    return cents if 0 < cents <= MAX_INCOME_CENTS else None


# what one user has chosen so far while setting up
@dataclass
class Draft:
    step: str
    framework: Framework | None = None
    take_home_cents: int | None = None
    period: str | None = None


# plan set up, display and removal over a few button taps
class PlanManager:
    # sessions for saving, the quota book for status and the locale for new users
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], book: QuotaBook, locale: Locale
    ) -> None:
        self._sessions = sessions
        self._book = book
        self._locale = locale
        self._drafts: dict[str, Draft] = {}

    # greet someone the first time they ever message
    async def welcome_if_new(self, inbound: Inbound, transport: Transport) -> None:
        async with self._sessions() as session:
            _, created = await ensure_user(session, inbound.user_id, self._locale)
            await session.commit()
        if created:
            await transport.send_buttons(
                inbound.user_id, WELCOME, [("plan:setup", "Set up a plan")]
            )

    # true while we are waiting for the income reply
    def waiting_for_text(self, wa_user_id: str) -> bool:
        draft = self._drafts.get(wa_user_id)
        return draft is not None and draft.step == "income"

    # show the current plan, or start set up when there is none
    async def show(self, inbound: Inbound, transport: Transport) -> None:
        async with self._sessions() as session:
            user_id, _ = await ensure_user(session, inbound.user_id, self._locale)
            await session.commit()
        report = await self._book.report(user_id)
        if report is None:
            await self.start(inbound, transport)
            return
        currency = report.user.currency
        parts = [
            f"- {b.name}: save *{money(b.quota_cents, currency)}*" if b.saving
            else f"- {b.name}: *{money(b.left_cents(), currency)}* left"
            for b in report.buckets
        ]
        preset = PRESETS.get(report.plan.framework)
        name = preset.name if preset else "Your own plan"
        text = f"*Your plan: {name}*\nThis {report.unit()}:\n" + "\n".join(parts)
        buttons = [("plan:change", "Change"), ("plan:remove", "Remove")]
        await transport.send_buttons(inbound.user_id, text, buttons)

    # first step, pick a framework
    async def start(self, inbound: Inbound, transport: Transport) -> None:
        self._drafts[inbound.user_id] = Draft("framework")
        buttons = [(f"plan:fw:{f.key}", f"{f.name} — {f.blurb}") for f in PRESETS.values()]
        await transport.send_buttons(inbound.user_id, PICK, buttons)

    # every plan button moves the set up one step
    async def on_button(self, inbound: Inbound, button_id: str, transport: Transport) -> None:
        user = inbound.user_id
        action = button_id.removeprefix("plan:")
        if action in ("setup", "change"):
            await self.start(inbound, transport)
            return
        if action == "cancel":
            self._drafts.pop(user, None)
            await transport.send_text(user, "*Okay, nothing changed.*")
            return
        if action == "remove":
            text = "*Remove your plan?*\nYour spending records stay."
            await transport.send_buttons(user, text, [("plan:remove:yes", "Yes, remove"), CANCEL])
            return
        if action == "remove:yes":
            await self._remove(inbound, transport)
            return
        draft = self._drafts.get(user)
        if draft is None:
            await transport.send_text(user, "*That set up has expired*\nLet's start again.")
            await self.start(inbound, transport)
            return
        await self._step(inbound, action, draft, transport)

    # the income reply
    async def on_text(self, inbound: Inbound, transport: Transport) -> None:
        draft = self._drafts[inbound.user_id]
        cents = parse_income(inbound.text or "")
        if cents is None:
            await transport.send_buttons(inbound.user_id, ASK_AGAIN, [CANCEL])
            return
        draft.take_home_cents = cents
        await self._ask_period(inbound, draft, transport)

    # framework, savings share, period, keep income and save
    async def _step(
        self, inbound: Inbound, action: str, draft: Draft, transport: Transport
    ) -> None:
        user = inbound.user_id
        if action.startswith("fw:") and action.removeprefix("fw:") in PRESETS:
            draft.framework = PRESETS[action.removeprefix("fw:")]
            if draft.framework.key == "pyf":
                draft.step = "savings"
                buttons = [(f"plan:pct:{n}", f"{n}%") for n in SAVE_CHOICES]
                text = "*How much do you want to save first?*\nThe rest is yours to spend."
                await transport.send_buttons(user, text, buttons)
                return
            await self._ask_income(inbound, draft, transport)
            return
        choice = action.removeprefix("pct:")
        if action.startswith("pct:") and choice.isdigit() and int(choice) in SAVE_CHOICES:
            draft.framework = pay_yourself_first(int(choice))
            await self._ask_income(inbound, draft, transport)
            return
        if action == "keep":
            draft.take_home_cents = await self._stored_income(user)
            await self._ask_period(inbound, draft, transport)
            return
        if action in ("weekly", "monthly"):
            draft.period = action
            await self._confirm(inbound, draft, transport)
            return
        if action == "save" and draft.step == "confirm":
            await self._save(inbound, draft, transport)
            return
        stale = "*That button is from an older message*\nSend /plan to start again."
        await transport.send_text(user, stale)

    # ask for income, offering the one we already know
    async def _ask_income(self, inbound: Inbound, draft: Draft, transport: Transport) -> None:
        draft.step = "income"
        known = await self._stored_income(inbound.user_id)
        buttons = [CANCEL]
        if known:
            currency = await self._currency(inbound.user_id)
            buttons = [("plan:keep", f"Keep {money(known, currency)}"), CANCEL]
        await transport.send_buttons(inbound.user_id, ASK_INCOME, buttons)

    # weekly or monthly
    async def _ask_period(self, inbound: Inbound, draft: Draft, transport: Transport) -> None:
        draft.step = "period"
        text = "*Check it every week or every month?*"
        buttons = [("plan:weekly", "Weekly"), ("plan:monthly", "Monthly"), CANCEL]
        await transport.send_buttons(inbound.user_id, text, buttons)

    # the plan in numbers before saving
    async def _confirm(self, inbound: Inbound, draft: Draft, transport: Transport) -> None:
        if draft.framework is None or not draft.take_home_cents or draft.period is None:
            await self.start(inbound, transport)
            return
        draft.step = "confirm"
        currency = await self._currency(inbound.user_id)
        unit = "week" if draft.period == "weekly" else "month"
        shares = [
            f"- {name}: *{money(quota_cents(draft.take_home_cents, pct, draft.period), currency)}*"
            for name, pct in draft.framework.buckets.items()
        ]
        lines = [
            f"*{draft.framework.name}*",
            f"On {money(draft.take_home_cents, currency)} a month, each {unit}:",
            *shares,
            "",
            f"_Based on {draft.framework.source}_"
        ]
        if draft.framework.key == "pyf":
            lines.append("_Tip: each time you get a raise, save 1% more._")
        buttons = [("plan:save", "Save"), ("plan:change", "Change"), CANCEL]
        await transport.send_buttons(inbound.user_id, "\n".join(lines), buttons)

    # store the plan and income, replacing any earlier plan
    async def _save(self, inbound: Inbound, draft: Draft, transport: Transport) -> None:
        framework = draft.framework
        if framework is None or draft.period is None:
            await self.start(inbound, transport)
            return
        values = {
            "framework": framework.key,
            "period": draft.period,
            "buckets": framework.buckets,
            "category_map": framework.category_map,
            "step_up_pct": 1 if framework.key == "pyf" else None
        }
        async with self._sessions() as session:
            user_id, _ = await ensure_user(session, inbound.user_id, self._locale)
            await session.execute(
                update(User).where(User.id == user_id).values(
                    take_home_cents=draft.take_home_cents
                )
            )
            await session.execute(
                insert(BudgetPlan)
                .values(user_id=user_id, **values)
                .on_conflict_do_update(index_elements=["user_id"], set_=values)
            )
            await session.commit()
        self._drafts.pop(inbound.user_id, None)
        text = "✅ *Plan saved*\nEach time you log, I'll show what's left."
        await transport.send_text(inbound.user_id, text)

    # delete the plan, spending records are untouched
    async def _remove(self, inbound: Inbound, transport: Transport) -> None:
        owner = select(User.id).where(User.wa_user_id == inbound.user_id).scalar_subquery()
        async with self._sessions() as session:
            await session.execute(delete(BudgetPlan).where(BudgetPlan.user_id == owner))
            await session.commit()
        self._drafts.pop(inbound.user_id, None)
        await transport.send_text(inbound.user_id, "*Plan removed.*\nYour spending records stay.")

    # income already stored for this user
    async def _stored_income(self, wa_user_id: str) -> int | None:
        query = select(User.take_home_cents).where(User.wa_user_id == wa_user_id)
        async with self._sessions() as session:
            return (await session.execute(query)).scalar_one_or_none()

    # currency for this user, the locale default before they exist
    async def _currency(self, wa_user_id: str) -> str:
        query = select(User.currency).where(User.wa_user_id == wa_user_id)
        async with self._sessions() as session:
            found = (await session.execute(query)).scalar_one_or_none()
        return found or self._locale.currency
