import uuid
from datetime import UTC, datetime

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from splitledger.contracts.messages import Inbound
from splitledger.contracts.transport import Transport
from splitledger.db.models import LedgerEntry, LedgerKind, User
from splitledger.handlers.log import money
from splitledger.pay.rails import PayRails

OWE_ALL = ("owe:all", "Everything I owe")
SQUARE = "You're all square with {}."
EARLIER = "With earlier bills that's *{}* in total."
MARKED = "✅ *Marked as paid · {} to {}*\nI've let {} know."
TOLD = "*{} paid you {}*\nCheck your bank app. If it hasn't arrived, tap below."
NOT_YOURS = "Only the person who was paid can say it hasn't arrived."
DONE_ALREADY = "That payment was already undone."
UNDONE = "Undone. {} still owes you {}."
MISSING = "*{} hasn't received {} yet*\nPlease check your transfer, then tap I've paid again."


# the i've paid button for one person and currency
def paid_button(creditor: uuid.UUID, currency: str) -> tuple[str, str]:
    return f"pay:done:{creditor}:{currency}", "I've paid"


# a person's name in messages
def name_of(user: User) -> str:
    return user.display_name or "your friend"


# pay links under you owe, i've paid clears the pair, only the person paid can undo it
class SettleUp:
    # sessions and the pay rails the locale turned on
    def __init__(self, sessions: async_sessionmaker[AsyncSession], rails: PayRails) -> None:
        self._sessions = sessions
        self._rails = rails

    # what user owes other in one currency across every bill, net of what other owes back
    async def owing(
        self, session: AsyncSession, user: uuid.UUID, other: uuid.UUID, currency: str
    ) -> int:
        signed = case(
            (LedgerEntry.kind == LedgerKind.RECEIVABLE, -LedgerEntry.amount_cents),
            else_=LedgerEntry.amount_cents
        )
        total = (await session.execute(
            select(func.coalesce(func.sum(signed), 0)).where(
                LedgerEntry.user_id == user, LedgerEntry.counterparty_id == other,
                LedgerEntry.currency == currency, LedgerEntry.voided_at.is_(None),
                LedgerEntry.kind.in_(
                    [LedgerKind.RECEIVABLE, LedgerKind.PAYABLE, LedgerKind.SETTLEMENT]
                )
            )
        )).scalar_one()
        return int(total)

    # the you owe message with how to pay and an i've paid button, then the qr if there is one
    async def ask(
        self, transport: Transport, debtor: User, creditor: User, currency: str, header: str,
        share: int, reference: str
    ) -> None:
        async with self._sessions() as session:
            cents = await self.owing(session, debtor.id, creditor.id, currency)
        if cents <= 0:
            await transport.send_buttons(debtor.wa_user_id, header, [OWE_ALL])
            return
        offer = self._rails.offer(creditor.wa_user_id, cents, currency, reference)
        text = header
        if cents != share:
            text += "\n" + EARLIER.format(money(cents, currency))
        buttons = [paid_button(creditor.id, currency), OWE_ALL]
        await transport.send_buttons(debtor.wa_user_id, f"{text}\n\n{offer.text}", buttons)
        if offer.qr is not None:
            caption = f"PayNow · {money(cents, currency)} to {name_of(creditor)}"
            await transport.send_image(debtor.wa_user_id, offer.qr, caption)

    # pay:done:<person>:<currency> from the debtor or pay:undo:<row> from the person paid
    async def on_button(self, inbound: Inbound, button_id: str, transport: Transport) -> None:
        parts = button_id.split(":")
        try:
            if parts[1] == "done" and len(parts) == 4:
                await self._paid(inbound, uuid.UUID(parts[2]), parts[3], transport)
            elif parts[1] == "undo" and len(parts) == 3:
                await self._undo(inbound, uuid.UUID(parts[2]), transport)
        except ValueError:
            return

    # post the payment on both sides and tell the person paid
    async def _paid(
        self, inbound: Inbound, creditor_id: uuid.UUID, currency: str, transport: Transport
    ) -> None:
        async with self._sessions() as session:
            debtor = (await session.execute(
                select(User).where(User.wa_user_id == inbound.user_id)
            )).scalar_one_or_none()
            creditor = await session.get(User, creditor_id)
            if debtor is None or creditor is None:
                return
            cents = await self.owing(session, debtor.id, creditor.id, currency)
            if cents <= 0:
                await transport.send_text(inbound.user_id, SQUARE.format(name_of(creditor)))
                return
            now = datetime.now(UTC)
            received = LedgerEntry(
                user_id=creditor.id, kind=LedgerKind.SETTLEMENT, amount_cents=cents,
                currency=currency, counterparty_id=debtor.id, note=debtor.display_name,
                occurred_at=now
            )
            session.add_all([received, LedgerEntry(
                user_id=debtor.id, kind=LedgerKind.SETTLEMENT, amount_cents=-cents,
                currency=currency, counterparty_id=creditor.id, note=creditor.display_name,
                occurred_at=now
            )])
            await session.commit()
        amount, them = money(cents, currency), name_of(creditor)
        await transport.send_text(inbound.user_id, MARKED.format(amount, them, them))
        await transport.send_buttons(
            creditor.wa_user_id, TOLD.format(name_of(debtor), amount),
            [(f"pay:undo:{received.id}", "Not received")]
        )

    # void both sides of a payment that never arrived, only for the person paid
    async def _undo(self, inbound: Inbound, row_id: uuid.UUID, transport: Transport) -> None:
        async with self._sessions() as session:
            row = await session.get(LedgerEntry, row_id)
            creditor = (await session.execute(
                select(User).where(User.wa_user_id == inbound.user_id)
            )).scalar_one_or_none()
            if row is None or creditor is None or row.user_id != creditor.id:
                await transport.send_text(inbound.user_id, NOT_YOURS)
                return
            if row.voided_at is not None:
                await transport.send_text(inbound.user_id, DONE_ALREADY)
                return
            twin = (await session.execute(
                select(LedgerEntry).where(
                    LedgerEntry.user_id == row.counterparty_id,
                    LedgerEntry.counterparty_id == creditor.id,
                    LedgerEntry.kind == LedgerKind.SETTLEMENT,
                    LedgerEntry.amount_cents == -row.amount_cents,
                    LedgerEntry.occurred_at == row.occurred_at,
                    LedgerEntry.voided_at.is_(None)
                )
            )).scalars().first()
            debtor = await session.get(User, row.counterparty_id) if row.counterparty_id else None
            now = datetime.now(UTC)
            row.voided_at = now
            if twin is not None:
                twin.voided_at = now
            await session.commit()
        amount = money(row.amount_cents, row.currency)
        who = name_of(debtor) if debtor else "your friend"
        await transport.send_text(inbound.user_id, UNDONE.format(who, amount))
        if debtor is None:
            return
        await transport.send_buttons(
            debtor.wa_user_id, MISSING.format(name_of(creditor), amount),
            [paid_button(creditor.id, row.currency), OWE_ALL]
        )
