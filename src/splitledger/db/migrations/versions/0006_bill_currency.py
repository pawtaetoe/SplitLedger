from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# a bill keeps the currency and category read from its receipt
def upgrade() -> None:
    op.add_column(
        "bills", sa.Column("currency", sa.String(), server_default="USD", nullable=False)
    )
    op.add_column("bills", sa.Column("category", sa.String()))


# drop them
def downgrade() -> None:
    op.drop_column("bills", "category")
    op.drop_column("bills", "currency")
