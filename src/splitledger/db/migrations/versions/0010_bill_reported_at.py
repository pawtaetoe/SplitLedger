from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# the last claim the payer has been told about
def upgrade() -> None:
    op.add_column("bills", sa.Column("reported_at", sa.DateTime(timezone=True)))


# drop it
def downgrade() -> None:
    op.drop_column("bills", "reported_at")
