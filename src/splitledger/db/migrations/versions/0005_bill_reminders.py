from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# optional reminders and paid tracking for planned bills
def upgrade() -> None:
    op.add_column(
        "planned_spends",
        sa.Column("remind", sa.Boolean(), server_default=sa.false(), nullable=False)
    )
    op.add_column("planned_spends", sa.Column("paid_through", sa.Date()))
    op.add_column("planned_spends", sa.Column("reminded_for", sa.Date()))


# drop them
def downgrade() -> None:
    for column in ("reminded_for", "paid_through", "remind"):
        op.drop_column("planned_spends", column)
