from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from splitledger.db.models import ChatState


# chat state in postgres, expired rows are ignored and cleared as new ones are written
class DbState:
    # sessions for the chat_state table
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    # the value under kind and key, none when missing or expired
    async def get(self, kind: str, key: str) -> dict[str, Any] | None:
        query = select(ChatState.value).where(
            ChatState.kind == kind, ChatState.key == key,
            ChatState.expires_at > datetime.now(UTC)
        )
        async with self._sessions() as session:
            return (await session.execute(query)).scalar_one_or_none()

    # store a value, replacing any earlier one, and clear anything already expired
    async def put(self, kind: str, key: str, value: dict[str, Any], ttl: timedelta) -> None:
        now = datetime.now(UTC)
        row = {"kind": kind, "key": key, "value": value, "expires_at": now + ttl}
        async with self._sessions() as session:
            await session.execute(delete(ChatState).where(ChatState.expires_at <= now))
            await session.execute(
                insert(ChatState).values(**row).on_conflict_do_update(
                    index_elements=["kind", "key"],
                    set_={"value": value, "expires_at": row["expires_at"]}
                )
            )
            await session.commit()

    # remove and return a value, none when missing or expired
    async def pop(self, kind: str, key: str) -> dict[str, Any] | None:
        query = (
            delete(ChatState)
            .where(ChatState.kind == kind, ChatState.key == key)
            .returning(ChatState.value, ChatState.expires_at)
        )
        async with self._sessions() as session:
            row = (await session.execute(query)).first()
            await session.commit()
        if row is None or row.expires_at <= datetime.now(UTC):
            return None
        return dict(row.value)
