from datetime import UTC, datetime, timedelta
from typing import Any


# chat state in memory for tests with no database
class MemoryState:
    # nothing stored yet
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], tuple[dict[str, Any], datetime]] = {}

    # the value under kind and key, none when missing or expired
    async def get(self, kind: str, key: str) -> dict[str, Any] | None:
        found = self.rows.get((kind, key))
        return found[0] if found and found[1] > datetime.now(UTC) else None

    # store a value until its ttl runs out
    async def put(self, kind: str, key: str, value: dict[str, Any], ttl: timedelta) -> None:
        self.rows[(kind, key)] = (value, datetime.now(UTC) + ttl)

    # remove and return a value
    async def pop(self, kind: str, key: str) -> dict[str, Any] | None:
        value = await self.get(kind, key)
        self.rows.pop((kind, key), None)
        return value
