from collections.abc import Sequence
from typing import TypeVar

from splitledger.contracts.llm import Tool, ToolRunResult

T = TypeVar("T")


# llm fake keyed by prompt name
class FakeLLM:
    # canned objects keyed by prompt name
    def __init__(self, canned: dict[str, object] | None = None) -> None:
        self.canned: dict[str, object] = canned or {}
        self.transcript: str = ""
        self.structured_calls: list[str] = []
        self.tool_result: ToolRunResult = ToolRunResult(text="")

    # return the canned object for this prompt
    async def structured(
        self, prompt: str, schema: type[T], images: Sequence[bytes] = ()
    ) -> T:
        self.structured_calls.append(prompt)
        key = prompt if prompt in self.canned else prompt.split(":", 1)[0]
        result = self.canned[key]
        assert isinstance(result, schema)
        return result

    # return the canned tool run result
    async def run_tools(
        self, system: str, user: str, tools: Sequence[Tool]
    ) -> ToolRunResult:
        return self.tool_result

    # return the canned transcript
    async def transcribe(self, audio: bytes, mime: str) -> str:
        return self.transcript
