from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# money limits per bucket for plans with envelopes
def upgrade() -> None:
    op.add_column(
        "budget_plans",
        sa.Column("caps", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False)
    )


# drop the limits
def downgrade() -> None:
    op.drop_column("budget_plans", "caps")
