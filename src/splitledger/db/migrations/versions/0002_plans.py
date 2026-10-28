from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# income columns and the budget plan table
def upgrade() -> None:
    op.add_column("users", sa.Column("take_home_cents", sa.BigInteger(), nullable=True))
    op.add_column("users", sa.Column("gross_income_cents", sa.BigInteger(), nullable=True))
    op.create_table(
        "budget_plans",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("framework", sa.String(), nullable=False),
        sa.Column("period", sa.String(), nullable=False),
        sa.Column("buckets", postgresql.JSONB(), nullable=False),
        sa.Column("category_map", postgresql.JSONB(), nullable=False),
        sa.Column("step_up_pct", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id")
    )


# drop the plan table and income columns
def downgrade() -> None:
    op.drop_table("budget_plans")
    op.drop_column("users", "gross_income_cents")
    op.drop_column("users", "take_home_cents")
