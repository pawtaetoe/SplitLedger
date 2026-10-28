import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from splitledger.contracts.llm import LLM
from splitledger.db.models import BudgetPlan, User
from splitledger.db.session import SessionFactory
from splitledger.handlers.plan import MODEL_TIMEOUT, PROMPT, parse_income
from splitledger.llm.client import MetaLLM
from splitledger.llm.schemas import CustomPlan
from splitledger.plans.custom import check_plan, fits_income
from splitledger.plans.frameworks import PRESETS, SAVE_CHOICES, Framework, pay_yourself_first
from splitledger.plans.store import plan_amounts, save_plan
from splitledger.settings import settings
from splitledger.split.tokens import ClaimTokens
from splitledger.web.claim import render
from splitledger.web.dashboard import SIGN_IN, DashboardPage, dashboard_page

LOG = logging.getLogger(__name__)
CHOICES = ("503020", "60", "pyf", "custom")
NEED_PAY = "Type what you take home a month, like 4500."
NEED_DRAFT = "Describe your plan, then tap Draft my plan."
TOO_MUCH = "Your shares and limits add up to more than you take home."
NO_DRAFT = "I couldn't read that just now. Try again, or pick a ready-made plan."


# what the form says, as typed
@dataclass(frozen=True)
class PlanForm:
    choice: str = "503020"
    save_pct: str = "20"
    describe: str = ""
    pay: str = ""
    period: str = "monthly"
    custom: str = ""


# the plan form with live amounts, saving the same plan the chat saves
class PlanPage:
    # sessions, the dashboard for sign in, a signer for drafted plans and the model
    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], dashboard: DashboardPage,
        tokens: ClaimTokens, llm: LLM
    ) -> None:
        self._sessions = sessions
        self._dashboard = dashboard
        self._tokens = tokens
        self._llm = llm

    # the form filled with the current plan and pay
    async def show(self, request: Request) -> HTMLResponse:
        user = await self._user(request)
        if user is None:
            return render("message.html", 401, title=SIGN_IN[0], body=SIGN_IN[1])
        async with self._sessions() as session:
            query = select(BudgetPlan).where(BudgetPlan.user_id == user.id)
            plan = (await session.execute(query)).scalar_one_or_none()
        form = PlanForm(pay=f"{user.take_home_cents / 100:g}" if user.take_home_cents else "")
        if plan is not None:
            form = self._from_plan(plan, form)
        return render("plan.html", form=form, user=user, **self._preview(form, user.currency))

    # the amounts as the form changes, no model call
    async def preview(self, request: Request, form: PlanForm) -> HTMLResponse:
        user = await self._user(request)
        if user is None:
            return render("message.html", 401, title=SIGN_IN[0], body=SIGN_IN[1])
        values = self._preview(form, user.currency)
        return render("plan_preview.html", form=form, user=user, **values)

    # Muse Spark drafts a plan from the words, python checks it, the result is signed
    async def draft(self, request: Request, form: PlanForm) -> HTMLResponse:
        user = await self._user(request)
        if user is None:
            return render("message.html", 401, title=SIGN_IN[0], body=SIGN_IN[1])
        prompt = f"custom_plan: {PROMPT}{form.describe}"
        try:
            drafted = await asyncio.wait_for(
                self._llm.structured(prompt, CustomPlan), MODEL_TIMEOUT
            )
        except Exception as error:
            LOG.warning("plan model failed with %s", type(error).__name__)
            values = {**self._preview(form, user.currency), "problem": NO_DRAFT}
            return render("plan_preview.html", form=form, user=user, **values)
        checked = check_plan(drafted)
        if checked.framework is None:
            values = {**self._preview(form, user.currency), "problem": checked.problem}
            return render("plan_preview.html", form=form, user=user, **values)
        period = drafted.period or form.period
        sealed = self._tokens.seal(json.dumps(as_json(checked.framework)))
        fresh = PlanForm("custom", form.save_pct, form.describe, form.pay, period, sealed)
        values = {**self._preview(fresh, user.currency), "notes": checked.notes}
        return render("plan_preview.html", form=fresh, user=user, **values)

    # save when everything checks out, else show the form with what is wrong
    async def save(self, request: Request, form: PlanForm) -> Response:
        user = await self._user(request)
        if user is None:
            return render("message.html", 401, title=SIGN_IN[0], body=SIGN_IN[1])
        values = self._preview(form, user.currency)
        framework, pay = self.framework_of(form), parse_income(form.pay)
        if values["problem"] or framework is None or pay is None:
            return render("plan.html", 400, form=form, user=user, **values)
        async with self._sessions() as session:
            await save_plan(session, user.id, framework, form.period, pay)
            await session.commit()
        return RedirectResponse("/me", 303)

    # the plan the form describes, none when a part is missing or edited
    def framework_of(self, form: PlanForm) -> Framework | None:
        if form.choice in ("503020", "60"):
            return PRESETS[form.choice]
        if form.choice == "pyf":
            pct = int(form.save_pct) if form.save_pct.isdigit() else 0
            return pay_yourself_first(pct) if pct in SAVE_CHOICES else None
        if form.choice == "custom" and form.custom:
            try:
                body = self._tokens.unseal(form.custom)
            except RuntimeError:
                return None
            return from_json(json.loads(body)) if body else None
        return None

    # amounts per part, or the one thing to fix
    def _preview(self, form: PlanForm, currency: str) -> dict[str, Any]:
        framework = self.framework_of(form)
        pay = parse_income(form.pay)
        period = form.period if form.period in ("weekly", "monthly") else "monthly"
        problem: str | None = None
        if framework is None:
            problem = NEED_DRAFT if form.choice == "custom" else "Pick a plan."
        elif pay is None:
            problem = NEED_PAY
        elif not fits_income(framework, pay):
            problem = TOO_MUCH
        amounts = plan_amounts(framework, pay, period) if framework and pay and not problem else []
        return {
            "framework": framework, "amounts": amounts, "problem": problem, "notes": [],
            "currency": currency, "unit": "week" if period == "weekly" else "month",
            "presets": PRESETS, "save_choices": SAVE_CHOICES
        }

    # the form as the saved plan would fill it
    def _from_plan(self, plan: BudgetPlan, form: PlanForm) -> PlanForm:
        if plan.framework in ("503020", "60"):
            return PlanForm(plan.framework, pay=form.pay, period=plan.period)
        if plan.framework == "pyf":
            save = str(plan.buckets.get("Savings", 20))
            return PlanForm("pyf", save, pay=form.pay, period=plan.period)
        framework = Framework(
            "custom", "Your own plan", "", "", plan.buckets, plan.category_map, plan.caps
        )
        sealed = self._tokens.seal(json.dumps(as_json(framework)))
        return PlanForm("custom", pay=form.pay, period=plan.period, custom=sealed)

    # the signed in user, none when not signed in
    async def _user(self, request: Request) -> User | None:
        user_id = self._dashboard.user_of(request)
        if user_id is None:
            return None
        async with self._sessions() as session:
            return await session.get(User, user_id)


# a drafted plan as plain values to sign
def as_json(framework: Framework) -> dict[str, object]:
    return {"buckets": framework.buckets, "map": framework.category_map, "caps": framework.caps}


# a signed drafted plan back into a framework
def from_json(body: dict[str, dict[str, object]]) -> Framework:
    buckets = {str(k): int(str(v)) for k, v in body["buckets"].items()}
    mapping = {str(k): str(v) for k, v in body["map"].items()}
    caps = {str(k): int(str(v)) for k, v in body["caps"].items()}
    return Framework("custom", "Your own plan", "", "", buckets, mapping, caps)


plan_page = PlanPage(SessionFactory, dashboard_page, ClaimTokens(settings.app_secret), MetaLLM())
plan_router = APIRouter()
Field = Annotated[str, Form()]


# the form fields as one object
def form_of(
    choice: Field = "503020", save_pct: Field = "20", describe: Field = "", pay: Field = "",
    period: Field = "monthly", custom: Field = ""
) -> PlanForm:
    return PlanForm(choice, save_pct, describe, pay, period, custom)


# the form
@plan_router.get("/plan", response_class=HTMLResponse)
async def page(request: Request) -> HTMLResponse:
    return await plan_page.show(request)


Filled = Annotated[PlanForm, Depends(form_of)]


# live amounts
@plan_router.post("/plan/preview", response_class=HTMLResponse)
async def preview(request: Request, form: Filled) -> HTMLResponse:
    return await plan_page.preview(request, form)


# draft a plan from words
@plan_router.post("/plan/draft", response_class=HTMLResponse)
async def draft(request: Request, form: Filled) -> HTMLResponse:
    return await plan_page.draft(request, form)


# save the plan
@plan_router.post("/plan/save")
async def save(request: Request, form: Filled) -> Response:
    return await plan_page.save(request, form)
