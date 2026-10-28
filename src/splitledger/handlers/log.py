import hashlib
import re
import uuid
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from splitledger.contracts.llm import LLM
from splitledger.contracts.messages import Inbound
from splitledger.contracts.transport import Transport
from splitledger.db.models import LedgerEntry, LedgerKind, LedgerSource, LedgerStatus, User
from splitledger.llm.schemas import CURRENCY_CODES, ExpenseDraft, Locale, ReceiptDraft

if TYPE_CHECKING:
    from splitledger.plans.quota import QuotaBook

PROMPTS = Path(__file__).parents[1] / "llm" / "prompts"
RECEIPT_PROMPT = f"parse_receipt: {(PROMPTS / 'parse_receipt.md').read_text(encoding='utf-8')}"
# cents a printed total may be off by before we ask
TOLERANCE = 5
MAX_VOICE_SECONDS = 60
MAX_VOICE_BYTES = 1_000_000
# optional currency symbol then an amount with up to two decimals
AMOUNT = re.compile(r"(?<![\w.])((?:[A-Z]{1,2})?[$€£¥₹])?(\d+(?:\.\d{1,2})?)(?![\w.])")


# one amount in the locale currency plus a known keyword, else none
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


# true when the lines and charges add up to the printed totals
def receipt_adds_up(receipt: ReceiptDraft) -> bool:
    if receipt.total_cents is None:
        return False
    lines = sum(item.price_cents for item in receipt.items)
    base = receipt.subtotal_cents if receipt.subtotal_cents is not None else lines
    if receipt.items and abs(lines - base) > TOLERANCE:
        return False
    # a receipt with only a total has nothing to cross check
    if not receipt.items and receipt.subtotal_cents is None:
        return True
    charges = receipt.service_cents + receipt.tax_cents - receipt.discount_cents
    return abs(base + charges - receipt.total_cents) <= TOLERANCE


# seconds an ogg opus clip plays, from the last page granule position at 48khz
def ogg_seconds(audio: bytes) -> float | None:
    last = audio.rfind(b"OggS")
    if last < 0 or len(audio) < last + 14:
        return None
    return int.from_bytes(audio[last + 6 : last + 14], "little", signed=True) / 48000


# too long by ogg length or by size, other formats only get the size cap until a decoder is allowed
def voice_too_long(audio: bytes) -> bool:
    seconds = ogg_seconds(audio)
    return len(audio) > MAX_VOICE_BYTES or (seconds is not None and seconds > MAX_VOICE_SECONDS)


# get or create a user, safe when two messages arrive together, true when just created
async def ensure_user(
    session: AsyncSession, wa_user_id: str, locale: Locale
) -> tuple[uuid.UUID, bool]:
    created = await session.execute(
        insert(User)
        .values(wa_user_id=wa_user_id, currency=locale.currency, timezone=locale.timezone)
        .on_conflict_do_nothing(index_elements=["wa_user_id"])
        .returning(User.id)
    )
    new_id = created.scalar_one_or_none()
    if new_id is not None:
        return new_id, True
    found = await session.execute(select(User.id).where(User.wa_user_id == wa_user_id))
    return found.scalar_one(), False


# money for a reply line, the code reads clearly in any country
def money(cents: int, currency: str) -> str:
    return f"{currency} {cents // 100}.{cents % 100:02d}"


# logs text and receipt expenses and undoes them
class ExpenseLogger:
    # model, ledger sessions, locale and the quota book that adds plan lines to replies
    def __init__(
        self, llm: LLM, sessions: async_sessionmaker[AsyncSession], locale: Locale,
        book: "QuotaBook | None" = None
    ) -> None:
        self._llm = llm
        self._sessions = sessions
        self._locale = locale
        self._book = book
        self._receipts: dict[str, ReceiptDraft] = {}
        self._logged: set[tuple[str, str]] = set()

    # save one expense and offer undo
    async def log(
        self, inbound: Inbound, transport: Transport, draft: ExpenseDraft,
        source: LedgerSource = LedgerSource.TEXT
    ) -> None:
        # the model can return zero or negative amounts so treat them as missing
        if draft.amount_cents is None or draft.amount_cents <= 0:
            await transport.send_text(
                inbound.user_id, "*How much was it?*\nTry something like _coffee 6.50_."
            )
            return
        await self._save(inbound, transport, draft, draft.amount_cents, source)

    # transcribe a voice note and echo what was heard, none when it cannot be used
    async def transcribe(self, inbound: Inbound, transport: Transport) -> str | None:
        audio = await transport.fetch_media(inbound.media_ref or "")
        if voice_too_long(audio):
            await transport.send_text(
                inbound.user_id,
                f"*That voice note is too long*\nPlease keep it under {MAX_VOICE_SECONDS} seconds."
            )
            return None
        # whatsapp voice notes are ogg opus
        heard = (await self._llm.transcribe(audio, "audio/ogg")).strip()
        if not heard:
            await transport.send_text(
                inbound.user_id, "*I couldn't hear that*\nTry again, or type it."
            )
            return None
        await transport.send_text(inbound.user_id, f"*I heard*\n> {heard}")
        return heard

    # read a receipt photo, save it when it adds up, else ask to confirm the total
    async def log_receipt(self, inbound: Inbound, transport: Transport) -> None:
        image = await transport.fetch_media(inbound.media_ref or "")
        key = hashlib.sha256(image).hexdigest()
        if (inbound.user_id, key) in self._logged:
            await transport.send_text(
                inbound.user_id, "*Already logged*\nThis receipt is saved already."
            )
            return
        # the same photo is never sent to the model twice
        receipt = self._receipts.get(key)
        if receipt is None:
            receipt = await self._llm.structured(RECEIPT_PROMPT, ReceiptDraft, images=[image])
            self._receipts[key] = receipt
        total = receipt.total_cents
        if total is None or total <= 0:
            await transport.send_text(
                inbound.user_id,
                "*I couldn't read the total*\nType it instead, like _lunch 23.40_."
            )
            return
        if not receipt_adds_up(receipt):
            shown = money(total, receipt.currency or self._locale.currency)
            await transport.send_buttons(
                inbound.user_id,
                "*Check the total*\nThe lines on this receipt don't add up.\n"
                f"Is the total *{shown}*?",
                [(f"receipt:{key}", "Yes, save it")]
            )
            return
        await self._save_receipt(inbound, transport, key)

    # save a receipt the user confirmed
    async def confirm_receipt(self, inbound: Inbound, key: str, transport: Transport) -> None:
        if key not in self._receipts or (inbound.user_id, key) in self._logged:
            await transport.send_text(inbound.user_id, "Nothing to save")
            return
        await self._save_receipt(inbound, transport, key)

    # turn a stored receipt into one expense
    async def _save_receipt(self, inbound: Inbound, transport: Transport, key: str) -> None:
        receipt = self._receipts[key]
        self._logged.add((inbound.user_id, key))
        draft = ExpenseDraft(
            amount_cents=receipt.total_cents,
            currency=receipt.currency,
            category=receipt.category,
            vendor=receipt.vendor
        )
        await self._save(inbound, transport, draft, receipt.total_cents or 0, LedgerSource.RECEIPT)

    # write one expense row and offer undo
    async def _save(
        self, inbound: Inbound, transport: Transport, draft: ExpenseDraft, amount: int,
        source: LedgerSource
    ) -> None:
        currency = draft.currency or self._locale.currency
        async with self._sessions() as session:
            user_id = await self._user_id(session, inbound.user_id)
            entry = LedgerEntry(
                user_id=user_id,
                kind=LedgerKind.EXPENSE,
                amount_cents=amount,
                currency=currency,
                category=draft.category,
                vendor=draft.vendor,
                note=draft.note,
                source=source,
                status=LedgerStatus.POSTED
            )
            session.add(entry)
            await session.commit()
        label = draft.vendor or draft.note or ""
        details = f"{draft.category} · {label}" if label else draft.category
        text = f"✅ *Logged {money(amount, currency)}*\n{details}"
        lines = await self._book.after_save(user_id, draft.category) if self._book else []
        if lines:
            text = text + "\n\n" + "\n".join(lines)
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

    # void the latest expense the sender logged themselves
    async def undo_last(self, inbound: Inbound, transport: Transport) -> None:
        query = (
            select(LedgerEntry.id)
            .join(User, LedgerEntry.user_id == User.id)
            .where(
                User.wa_user_id == inbound.user_id,
                LedgerEntry.kind == LedgerKind.EXPENSE,
                LedgerEntry.source != LedgerSource.BILL,
                LedgerEntry.voided_at.is_(None)
            )
            .order_by(LedgerEntry.occurred_at.desc())
            .limit(1)
        )
        async with self._sessions() as session:
            latest = (await session.execute(query)).scalar_one_or_none()
        await self.undo(inbound, str(latest or ""), transport)

    # get or create the user and return their id
    async def _user_id(self, session: AsyncSession, wa_user_id: str) -> uuid.UUID:
        return (await ensure_user(session, wa_user_id, self._locale))[0]
