from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# people sharing a bill evenly instead of claiming items
def upgrade() -> None:
    op.add_column("bills", sa.Column("even_ways", sa.Integer()))


# drop it
def downgrade() -> None:
    op.drop_column("bills", "even_ways")
