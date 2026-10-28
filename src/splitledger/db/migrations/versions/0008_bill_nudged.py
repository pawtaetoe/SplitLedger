from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# when the payer was last asked to close an open bill
def upgrade() -> None:
    op.add_column("bills", sa.Column("nudged_at", sa.DateTime(timezone=True)))


# drop it
def downgrade() -> None:
    op.drop_column("bills", "nudged_at")
