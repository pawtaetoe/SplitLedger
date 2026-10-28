from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# when anyone last claimed, joined or changed a claim on the bill
def upgrade() -> None:
    op.add_column("bills", sa.Column("claimed_at", sa.DateTime(timezone=True)))


# drop it
def downgrade() -> None:
    op.drop_column("bills", "claimed_at")
