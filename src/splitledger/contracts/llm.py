from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

T = TypeVar("T")


# one tool the model may call
@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    run: Callable[[dict[str, Any]], Awaitable[Any]]


# outcome of one tool calling run
@dataclass(frozen=True)
class ToolRunResult:
    text: str
    steps: int = 0
    calls: list[str] = field(default_factory=list)


# model access behind one seam
class LLM(Protocol):
    # return one schema validated object
    async def structured(
        self, prompt: str, schema: type[T], images: Sequence[bytes] = ()
    ) -> T: ...

    # run tools until the model answers
    async def run_tools(
        self, system: str, user: str, tools: Sequence[Tool]
    ) -> ToolRunResult: ...

    # transcribe one audio clip
    async def transcribe(self, audio: bytes, mime: str) -> str: ...
