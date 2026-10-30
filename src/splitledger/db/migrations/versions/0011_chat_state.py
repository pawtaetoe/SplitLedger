from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# short lived chat state like drafts and receipts read, so a restart loses nothing
def upgrade() -> None:
    op.create_table(
        "chat_state",
        sa.Column("kind", sa.String, primary_key=True),
        sa.Column("key", sa.String, primary_key=True),
        sa.Column("value", JSONB, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False)
    )
    op.create_index("ix_chat_state_expires_at", "chat_state", ["expires_at"])


# drop it
def downgrade() -> None:
    op.drop_index("ix_chat_state_expires_at", "chat_state")
    op.drop_table("chat_state")
