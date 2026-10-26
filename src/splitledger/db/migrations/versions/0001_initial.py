from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ledger_kind = postgresql.ENUM("EXPENSE", "RECEIVABLE", "PAYABLE", "SETTLEMENT", name="ledger_kind")
ledger_source = postgresql.ENUM("TEXT", "RECEIPT", "VOICE", "BILL", name="ledger_source")
ledger_status = postgresql.ENUM("PENDING", "POSTED", name="ledger_status")
bill_status = postgresql.ENUM("OPEN", "FINALISED", "SETTLED", name="bill_status")
paid_status = postgresql.ENUM("UNPAID", "PAID", name="paid_status")
NOW = sa.func.now()


# create all core tables
def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("wa_user_id", sa.String(), nullable=False),
        sa.Column("phone", sa.String(), nullable=True),
        sa.Column("display_name", sa.String(), nullable=True),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("timezone", sa.String(), nullable=False),
        sa.Column("pay_methods", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("id")
    )
    op.create_index("ix_users_wa_user_id", "users", ["wa_user_id"], unique=True)
    op.create_table(
        "processed_messages",
        sa.Column("wa_message_id", sa.String(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.PrimaryKeyConstraint("wa_message_id")
    )
    op.create_table(
        "bills",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=6), nullable=False),
        sa.Column("payer_id", sa.Uuid(), nullable=False),
        sa.Column("vendor", sa.String(), nullable=True),
        sa.Column("subtotal", sa.BigInteger(), nullable=False),
        sa.Column("service", sa.BigInteger(), nullable=False),
        sa.Column("tax", sa.BigInteger(), nullable=False),
        sa.Column("discount", sa.BigInteger(), nullable=False),
        sa.Column("total", sa.BigInteger(), nullable=False),
        sa.Column("status", bill_status, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("finalised_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["payer_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id")
    )
    op.create_index("ix_bills_code", "bills", ["code"], unique=True)
    op.create_index("ix_bills_payer_id", "bills", ["payer_id"])
    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", ledger_kind, nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("vendor", sa.String(), nullable=True),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=NOW, nullable=False),
        sa.Column("source", ledger_source, nullable=False),
        sa.Column("bill_id", sa.Uuid(), nullable=True),
        sa.Column("counterparty_id", sa.Uuid(), nullable=True),
        sa.Column("status", ledger_status, nullable=False),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["bill_id"], ["bills.id"]),
        sa.ForeignKeyConstraint(["counterparty_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id")
    )
    op.create_index("ix_ledger_entries_user_id", "ledger_entries", ["user_id"])
    op.create_index(
        "ix_ledger_user_kind_occurred", "ledger_entries", ["user_id", "kind", "occurred_at"]
    )
    op.create_table(
        "budgets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("month", sa.String(), nullable=False),
        sa.Column("limit_cents", sa.BigInteger(), nullable=False),
        sa.Column("last_alert_pct", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "category", "month", name="uq_budget_month")
    )
    op.create_index("ix_budgets_user_id", "budgets", ["user_id"])
    op.create_table(
        "bill_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("bill_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("price_cents", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["bill_id"], ["bills.id"]),
        sa.PrimaryKeyConstraint("id")
    )
    op.create_index("ix_bill_items_bill_id", "bill_items", ["bill_id"])
    op.create_table(
        "participants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("bill_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("guest_name", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["bill_id"], ["bills.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id")
    )
    op.create_index("ix_participants_bill_id", "participants", ["bill_id"])
    op.create_index("ix_participants_user_id", "participants", ["user_id"])
    op.create_table(
        "item_claims",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("bill_item_id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["bill_item_id"], ["bill_items.id"]),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bill_item_id", "participant_id", name="uq_claim_pair")
    )
    op.create_index("ix_item_claims_bill_item_id", "item_claims", ["bill_item_id"])
    op.create_index("ix_item_claims_participant_id", "item_claims", ["participant_id"])
    op.create_table(
        "allocations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("bill_id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("paid_status", paid_status, nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["bill_id"], ["bills.id"]),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("bill_id", "participant_id", name="uq_alloc_pair")
    )
    op.create_index("ix_allocations_bill_id", "allocations", ["bill_id"])
    op.create_index("ix_allocations_participant_id", "allocations", ["participant_id"])


# drop all core tables and enums
def downgrade() -> None:
    op.drop_table("allocations")
    op.drop_table("item_claims")
    op.drop_table("participants")
    op.drop_table("bill_items")
    op.drop_table("budgets")
    op.drop_table("ledger_entries")
    op.drop_table("bills")
    op.drop_table("processed_messages")
    op.drop_table("users")
    paid_status.drop(op.get_bind(), checkfirst=True)
    bill_status.drop(op.get_bind(), checkfirst=True)
    ledger_status.drop(op.get_bind(), checkfirst=True)
    ledger_source.drop(op.get_bind(), checkfirst=True)
    ledger_kind.drop(op.get_bind(), checkfirst=True)
