import re
import uuid
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from splitledger.contracts.llm import LLM
from splitledger.contracts.messages import Inbound
from splitledger.contracts.transport import Transport
from splitledger.db.models import LedgerEntry, LedgerKind, LedgerSource, LedgerStatus, User
from splitledger.llm.schemas import CURRENCY_CODES, ExpenseDraft, Locale

PROMPT = (Path(__file__).parents[1] / "llm" / "prompts" / "parse_expense.md").read_text(
    encoding="utf-8"
)
# optional currency symbol then an amount with up to two decimals
AMOUNT = re.compile(r"(?<![\w.])((?:[A-Z]{1,2})?[$€£¥₹])?(\d+(?:\.\d{1,2})?)(?![\w.])")


# one amount in the locale currency plus a known keyword, else none so the model decides
def quick_parse(text: str, locale: Locale) -> ExpenseDraft | None:
    amounts = AMOUNT.findall(text)
    if len(amounts) != 1:
        return None
    symbol, amount = amounts[0]
    if symbol and symbol not in locale.symbols:
        return None
    note = " ".join(AMOUNT.sub(" ", text).split()).lower()
    words = set(re.findall(r"[a-z]+", note))
    if words & CURRENCY_CODES:
        return None
    for category, keywords in locale.words.items():
        if words.intersection(keywords):
            cents = int(Decimal(amount) * 100)
            return ExpenseDraft(amount_cents=cents, category=category, note=note)
    return None


# money for a reply line, the code reads clearly in any country
def money(cents: int, currency: str) -> str:
    return f"{currency} {cents // 100}.{cents % 100:02d}"


# logs text expenses and undoes them
class ExpenseLogger:
    # model for messy text, a session factory for the ledger and the locale for keywords
    def __init__(
        self, llm: LLM, sessions: async_sessionmaker[AsyncSession], locale: Locale
    ) -> None:
        self._llm = llm
        self._sessions = sessions
        self._locale = locale

    # fast path first, then the model
    async def parse(self, text: str) -> ExpenseDraft:
        draft = quick_parse(text, self._locale)
        if draft is not None:
            return draft
        return await self._llm.structured(f"parse_expense: {PROMPT}{text}", ExpenseDraft)

    # save one expense and offer undo
    async def log(self, inbound: Inbound, transport: Transport) -> None:
        draft = await self.parse(inbound.text or "")
        # the model can return zero or negative amounts so treat them as missing
        if draft.amount_cents is None or draft.amount_cents <= 0:
            await transport.send_text(
                inbound.user_id, "How much was it? Try something like coffee 6.50"
            )
            return
        currency = draft.currency or self._locale.currency
        async with self._sessions() as session:
            entry = LedgerEntry(
                user_id=await self._user_id(session, inbound.user_id),
                kind=LedgerKind.EXPENSE,
                amount_cents=draft.amount_cents,
                currency=currency,
                category=draft.category,
                vendor=draft.vendor,
                note=draft.note,
                source=LedgerSource.TEXT,
                status=LedgerStatus.POSTED
            )
            session.add(entry)
            await session.commit()
        label = draft.vendor or draft.note or ""
        summary = f"Logged {money(draft.amount_cents, currency)} · {draft.category}"
        text = f"{summary} · {label}" if label else summary
        await transport.send_buttons(inbound.user_id, text, [(f"undo:{entry.id}", "Undo")])

    # void one entry the sender owns, repeat taps change nothing
    async def undo(self, inbound: Inbound, entry_id: str, transport: Transport) -> None:
        try:
            target = uuid.UUID(entry_id)
        except ValueError:
            await transport.send_text(inbound.user_id, "Nothing to undo")
            return
        owner = select(User.id).where(User.wa_user_id == inbound.user_id).scalar_subquery()
        query = (
            update(LedgerEntry)
            .where(
                LedgerEntry.id == target,
                LedgerEntry.user_id == owner,
                LedgerEntry.voided_at.is_(None)
            )
            .values(voided_at=func.now())
            .returning(LedgerEntry.id)
            .execution_options(synchronize_session=False)
        )
        async with self._sessions() as session:
            voided = (await session.execute(query)).scalar_one_or_none()
            await session.commit()
        await transport.send_text(inbound.user_id, "Removed" if voided else "Nothing to undo")

    # get or create the user, safe when two messages arrive together
    async def _user_id(self, session: AsyncSession, wa_user_id: str) -> uuid.UUID:
        await session.execute(
            insert(User).values(wa_user_id=wa_user_id).on_conflict_do_nothing(
                index_elements=["wa_user_id"]
            )
        )
        found = await session.execute(select(User.id).where(User.wa_user_id == wa_user_id))
        return found.scalar_one()
