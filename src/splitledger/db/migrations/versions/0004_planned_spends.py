from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# bills and purchases the user expects, once or on repeat
def upgrade() -> None:
    op.create_table(
        "planned_spends",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("category", sa.String()),
        sa.Column("every", sa.String(), nullable=False),
        sa.Column("due_on", sa.Date(), nullable=False),
        sa.Column("voided_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        )
    )
    op.create_index("ix_planned_spends_user_id", "planned_spends", ["user_id"])


# drop the table
def downgrade() -> None:
    op.drop_index("ix_planned_spends_user_id", "planned_spends")
    op.drop_table("planned_spends")
