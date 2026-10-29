import asyncio
import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from splitledger.contracts.llm import LLM
from splitledger.contracts.messages import Inbound
from splitledger.contracts.transport import Transport
from splitledger.db.models import BudgetPlan, User
from splitledger.handlers.dashboard import SEE_ALL, DashboardLink
from splitledger.handlers.log import ensure_user, money
from splitledger.llm.schemas import CustomPlan, Locale
from splitledger.plans.custom import check_plan, fits_income
from splitledger.plans.frameworks import PRESETS, SAVE_CHOICES, Framework, pay_yourself_first
from splitledger.plans.quota import QuotaBook
from splitledger.plans.store import plan_amounts, save_plan

WELCOME = (
    "*Hi! I help with your money, right here in WhatsApp.*\n"
    "- *Log spending:* type it, like _coffee 4.50_, or send a receipt photo or voice note\n"
    "- *Split a bill:* send the receipt photo with the caption _split_\n"
    "- *Plan:* set a budget and I'll show what's left after each log"
)
LOG = logging.getLogger(__name__)
PROMPT = (Path(__file__).parents[1] / "llm" / "prompts" / "custom_plan.md").read_text(
    encoding="utf-8"
)
# seconds to wait for the model to draft a plan
MODEL_TIMEOUT = 15.0
PICK = "*Pick a plan*\nYou can change it any time."
MORE = ("plan:more", "More choices — 60% solution, or describe your own")
OWN = ("plan:fw:custom", "Describe my own — your own split, or limits per category")
EXAMPLE = "save 30%, food under 400 a month"
DESCRIBE = (
    "*Describe your plan*\n"
    f"For example: _{EXAMPLE}_\n"
    "or _50/30/20 but save 25%_."
)
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
    notes: list[str] = field(default_factory=list)
    failures: int = 0


# plan set up, display and removal over a few button taps
class PlanManager:
    # sessions, the quota book, the locale for new users, the model for own plans and page links
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], book: QuotaBook, locale: Locale,
        llm: LLM, links: DashboardLink | None = None
    ) -> None:
        self._sessions = sessions
        self._book = book
        self._locale = locale
        self._llm = llm
        self._links = links
        self._drafts: dict[str, Draft] = {}

    # greet someone the first time they ever message
    async def welcome_if_new(self, inbound: Inbound, transport: Transport) -> None:
        async with self._sessions() as session:
            _, created = await ensure_user(session, inbound.user_id, self._locale)
            await session.commit()
        if created:
            choices = [
                ("menu:split", "Split a bill"), ("plan:setup", "Set up a plan"),
                ("menu:open", "See what I can do")
            ]
            await transport.send_buttons(inbound.user_id, WELCOME, choices)

    # true while we are waiting for a typed plan or income reply
    def waiting_for_text(self, wa_user_id: str) -> bool:
        draft = self._drafts.get(wa_user_id)
        return draft is not None and draft.step in ("describe", "income")

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
        buttons = [
            ("plan:change", "Change"), ("plan:web", "Change on a page"), ("plan:remove", "Remove")
        ]
        await transport.send_buttons(inbound.user_id, text, buttons)

    # first step, pick a framework
    async def start(self, inbound: Inbound, transport: Transport) -> None:
        self._drafts[inbound.user_id] = Draft("framework")
        buttons = [(f"plan:fw:{f.key}", f"{f.name} — {f.blurb}") for f in PRESETS.values()]
        # three real buttons at most, so the rest sit behind more choices
        first = [b for b in buttons if b[0] != "plan:fw:60"]
        await transport.send_buttons(inbound.user_id, PICK, [*first, MORE])

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

    # a typed plan or the income reply
    async def on_text(self, inbound: Inbound, transport: Transport) -> None:
        draft = self._drafts[inbound.user_id]
        if draft.step == "describe":
            await self._describe(inbound, draft, transport)
            return
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
        if action == "more":
            sixty = PRESETS["60"]
            buttons = [(f"plan:fw:{sixty.key}", f"{sixty.name} — {sixty.blurb}"), OWN]
            on_page = ("plan:web", "Set up on a page — every choice on one screen")
            await transport.send_buttons(user, "*More choices*", [*buttons, on_page])
            return
        if action == "fw:custom":
            draft.step = "describe"
            await transport.send_buttons(user, DESCRIBE, [CANCEL])
            return
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

    # the model drafts a plan from words, python checks it before anything is saved
    async def _describe(self, inbound: Inbound, draft: Draft, transport: Transport) -> None:
        user = inbound.user_id
        prompt = f"custom_plan: {PROMPT}{inbound.text or ''}"
        try:
            plan = await asyncio.wait_for(
                self._llm.structured(prompt, CustomPlan), MODEL_TIMEOUT
            )
        except Exception as error:
            LOG.warning("plan model failed with %s", type(error).__name__)
            text = "*I couldn't read that just now*\nTry again, or pick a ready-made plan."
            await transport.send_buttons(user, text, [("plan:setup", "Ready-made plans"), CANCEL])
            return
        checked = check_plan(plan)
        if checked.framework is None:
            await self._refuse(inbound, draft, checked.problem or "", transport)
            return
        draft.framework = checked.framework
        draft.notes = checked.notes
        draft.period = plan.period
        await self._ask_income(inbound, draft, transport)

    # say what is wrong, and after two misses offer the ready-made plans
    async def _refuse(
        self, inbound: Inbound, draft: Draft, problem: str, transport: Transport
    ) -> None:
        draft.failures += 1
        draft.step = "describe"
        if draft.failures >= 2:
            text = f"*Let's try a ready-made plan*\n{problem}"
            await transport.send_text(inbound.user_id, text)
            await self.start(inbound, transport)
            return
        text = f"*That plan doesn't work yet*\n{problem}\nTry again, like _{EXAMPLE}_."
        await transport.send_buttons(inbound.user_id, text, [CANCEL])

    # weekly or monthly, skipped when the plan already says
    async def _ask_period(self, inbound: Inbound, draft: Draft, transport: Transport) -> None:
        if draft.period is not None:
            await self._confirm(inbound, draft, transport)
            return
        draft.step = "period"
        text = "*Check it every week or every month?*"
        buttons = [("plan:weekly", "Weekly"), ("plan:monthly", "Monthly"), CANCEL]
        await transport.send_buttons(inbound.user_id, text, buttons)

    # the plan in numbers before saving
    async def _confirm(self, inbound: Inbound, draft: Draft, transport: Transport) -> None:
        if draft.framework is None or not draft.take_home_cents or draft.period is None:
            await self.start(inbound, transport)
            return
        framework = draft.framework
        if not fits_income(framework, draft.take_home_cents):
            problem = "Your shares and limits add up to more than you take home."
            await self._refuse(inbound, draft, problem, transport)
            return
        draft.step = "confirm"
        currency = await self._currency(inbound.user_id)
        unit = "week" if draft.period == "weekly" else "month"
        shares = [
            f"- {name}: *{money(quota, currency)}*"
            for name, quota, _ in plan_amounts(framework, draft.take_home_cents, draft.period)
        ]
        credit = f"_Based on {framework.source}_" if framework.source else (
            "_Your own plan, drafted with Muse Spark_"
        )
        lines = [
            f"*{framework.name}*",
            f"On {money(draft.take_home_cents, currency)} a month, each {unit}:",
            *shares,
            "",
            *[f"_{note}_" for note in draft.notes],
            credit
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
        async with self._sessions() as session:
            user_id, _ = await ensure_user(session, inbound.user_id, self._locale)
            await save_plan(
                session, user_id, framework, draft.period, draft.take_home_cents or 0
            )
            await session.commit()
        self._drafts.pop(inbound.user_id, None)
        text = "✅ *Plan saved*\nEach time you log, I'll show what's left."
        if self._links is None:
            await transport.send_text(inbound.user_id, text)
            return
        await transport.send_link(inbound.user_id, text, SEE_ALL, self._links.url(user_id))

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
