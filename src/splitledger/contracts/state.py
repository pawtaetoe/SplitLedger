from datetime import timedelta
from typing import Any, Protocol


# short lived chat state that must survive a restart, each value expires after its ttl
class State(Protocol):
    # the value under kind and key, none when missing or expired
    async def get(self, kind: str, key: str) -> dict[str, Any] | None: ...

    # store a value, replacing any earlier one under the same kind and key
    async def put(self, kind: str, key: str, value: dict[str, Any], ttl: timedelta) -> None: ...

    # remove and return a value, none when missing or expired
    async def pop(self, kind: str, key: str) -> dict[str, Any] | None: ...
