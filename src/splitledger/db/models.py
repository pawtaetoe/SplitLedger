import enum
import uuid
from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


# base for all tables
class Base(DeclarativeBase):
    pass


# ledger entry kinds
class LedgerKind(enum.StrEnum):
    EXPENSE = "EXPENSE"
    RECEIVABLE = "RECEIVABLE"
    PAYABLE = "PAYABLE"
    SETTLEMENT = "SETTLEMENT"


# where a ledger entry came from
class LedgerSource(enum.StrEnum):
    TEXT = "TEXT"
    RECEIPT = "RECEIPT"
    VOICE = "VOICE"
    BILL = "BILL"


# ledger entry states
class LedgerStatus(enum.StrEnum):
    PENDING = "PENDING"
    POSTED = "POSTED"


# bill lifecycle states
class BillStatus(enum.StrEnum):
    OPEN = "OPEN"
    FINALISED = "FINALISED"
    SETTLED = "SETTLED"


# allocation payment states
class PaidStatus(enum.StrEnum):
    UNPAID = "UNPAID"
    PAID = "PAID"


# one whatsapp user
class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    wa_user_id: Mapped[str] = mapped_column(sa.String, unique=True, index=True)
    phone: Mapped[str | None] = mapped_column(sa.String)
    display_name: Mapped[str | None] = mapped_column(sa.String)
    currency: Mapped[str] = mapped_column(sa.String, default="SGD")
    timezone: Mapped[str] = mapped_column(sa.String, default="Asia/Singapore")
    pay_methods: Mapped[dict[str, str]] = mapped_column(JSONB, default=dict)
    take_home_cents: Mapped[int | None] = mapped_column(sa.BigInteger)
    gross_income_cents: Mapped[int | None] = mapped_column(sa.BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


# a bill or purchase the user expects, once or on repeat, never deleted
class PlannedSpend(Base):
    __tablename__ = "planned_spends"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id"), index=True)
    label: Mapped[str] = mapped_column(sa.String)
    amount_cents: Mapped[int] = mapped_column(sa.BigInteger)
    currency: Mapped[str] = mapped_column(sa.String)
    category: Mapped[str | None] = mapped_column(sa.String)
    # once, week, month or year
    every: Mapped[str] = mapped_column(sa.String)
    # the day for a one off, the first due day for a repeat
    due_on: Mapped[date] = mapped_column(sa.Date)
    # message the day before each due day
    remind: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    # due days up to and including this one are paid
    paid_through: Mapped[date | None] = mapped_column(sa.Date)
    # the due day we last sent a reminder for
    reminded_for: Mapped[date | None] = mapped_column(sa.Date)
    voided_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


# one ledger row, never deleted
class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    __table_args__ = (sa.Index("ix_ledger_user_kind_occurred", "user_id", "kind", "occurred_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id"), index=True)
    kind: Mapped[LedgerKind] = mapped_column(sa.Enum(LedgerKind, name="ledger_kind"))
    amount_cents: Mapped[int] = mapped_column(sa.BigInteger)
    category: Mapped[str | None] = mapped_column(sa.String)
    vendor: Mapped[str | None] = mapped_column(sa.String)
    note: Mapped[str | None] = mapped_column(sa.String)
    currency: Mapped[str] = mapped_column(sa.String, default="SGD")
    occurred_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    source: Mapped[LedgerSource] = mapped_column(
        sa.Enum(LedgerSource, name="ledger_source"), default=LedgerSource.TEXT
    )
    bill_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("bills.id"))
    counterparty_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("users.id"))
    status: Mapped[LedgerStatus] = mapped_column(
        sa.Enum(LedgerStatus, name="ledger_status"), default=LedgerStatus.POSTED
    )
    voided_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


# dedupe key for inbound messages
class ProcessedMessage(Base):
    __tablename__ = "processed_messages"

    wa_message_id: Mapped[str] = mapped_column(sa.String, primary_key=True)
    received_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


# one budget for one month
class Budget(Base):
    __tablename__ = "budgets"
    __table_args__ = (sa.UniqueConstraint("user_id", "category", "month", name="uq_budget_month"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id"), index=True)
    category: Mapped[str | None] = mapped_column(sa.String)
    month: Mapped[str] = mapped_column(sa.String)
    limit_cents: Mapped[int] = mapped_column(sa.BigInteger)
    last_alert_pct: Mapped[int] = mapped_column(sa.Integer, default=0)


# the budget framework a user follows, at most one each
class BudgetPlan(Base):
    __tablename__ = "budget_plans"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id"), unique=True)
    framework: Mapped[str] = mapped_column(sa.String)
    period: Mapped[str] = mapped_column(sa.String)
    buckets: Mapped[dict[str, int]] = mapped_column(JSONB)
    category_map: Mapped[dict[str, str]] = mapped_column(JSONB)
    caps: Mapped[dict[str, int]] = mapped_column(JSONB, default=dict)
    step_up_pct: Mapped[int | None] = mapped_column(sa.Integer)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


# one group bill, money columns in cents
class Bill(Base):
    __tablename__ = "bills"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(sa.String(6), unique=True, index=True)
    payer_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("users.id"), index=True)
    vendor: Mapped[str | None] = mapped_column(sa.String)
    currency: Mapped[str] = mapped_column(sa.String)
    category: Mapped[str | None] = mapped_column(sa.String)
    # people sharing the total evenly, none when friends claim items
    even_ways: Mapped[int | None] = mapped_column(sa.Integer)
    subtotal: Mapped[int] = mapped_column(sa.BigInteger, default=0)
    service: Mapped[int] = mapped_column(sa.BigInteger, default=0)
    tax: Mapped[int] = mapped_column(sa.BigInteger, default=0)
    discount: Mapped[int] = mapped_column(sa.BigInteger, default=0)
    total: Mapped[int] = mapped_column(sa.BigInteger)
    status: Mapped[BillStatus] = mapped_column(
        sa.Enum(BillStatus, name="bill_status"), default=BillStatus.OPEN
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    finalised_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


# one line on a bill
class BillItem(Base):
    __tablename__ = "bill_items"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    bill_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("bills.id"), index=True)
    name: Mapped[str] = mapped_column(sa.String)
    qty: Mapped[int] = mapped_column(sa.Integer, default=1)
    price_cents: Mapped[int] = mapped_column(sa.BigInteger)
    position: Mapped[int] = mapped_column(sa.Integer, default=0)


# one person on a bill
class Participant(Base):
    __tablename__ = "participants"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    bill_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("bills.id"), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(sa.ForeignKey("users.id"), index=True)
    guest_name: Mapped[str] = mapped_column(sa.String)


# one claim of an item by a participant
class ItemClaim(Base):
    __tablename__ = "item_claims"
    __table_args__ = (sa.UniqueConstraint("bill_item_id", "participant_id", name="uq_claim_pair"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    bill_item_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("bill_items.id"), index=True)
    participant_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("participants.id"), index=True)
    weight: Mapped[float] = mapped_column(sa.Float, default=1.0)


# final share owed per participant
class Allocation(Base):
    __tablename__ = "allocations"
    __table_args__ = (sa.UniqueConstraint("bill_id", "participant_id", name="uq_alloc_pair"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    bill_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("bills.id"), index=True)
    participant_id: Mapped[uuid.UUID] = mapped_column(sa.ForeignKey("participants.id"), index=True)
    amount_cents: Mapped[int] = mapped_column(sa.BigInteger)
    paid_status: Mapped[PaidStatus] = mapped_column(
        sa.Enum(PaidStatus, name="paid_status"), default=PaidStatus.UNPAID
    )
    posted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
