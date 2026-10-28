import asyncio
import base64
import hashlib
import json
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TypeVar, cast

import httpx
from pydantic import BaseModel

from splitledger.contracts.llm import Tool, ToolRunResult
from splitledger.settings import settings

T = TypeVar("T")

LOG = logging.getLogger(__name__)
MAX_TOOL_ROUNDS = 4


# fixture file for one prompt and its images
def fixture_path(fixtures_dir: Path, prompt: str, images: Sequence[bytes] = ()) -> Path:
    raw_name = prompt.split(":", 1)[0]
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in raw_name)
    digest = hashlib.sha256(prompt.encode())
    for image in images:
        digest.update(image)
    return fixtures_dir / f"{safe[:40] or 'prompt'}-{digest.hexdigest()[:12]}.json"


# meta model api client behind the llm contract
class MetaLLM:
    # credentials and model default from settings
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        fixtures_dir: Path | None = None,
        backoff: float = 0.5
    ) -> None:
        self._key = api_key or settings.meta_api_key
        self._base = (base_url or settings.llm_base_url).rstrip("/")
        self._model = model or settings.llm_model
        self._fixtures = fixtures_dir
        self._backoff = backoff

    # auth header for api calls
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}"}

    # post with retries on transient failures
    async def _post(self, url: str, **kwargs: Any) -> httpx.Response:
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    response = await client.post(url, headers=self._headers(), **kwargs)
            except httpx.TransportError:
                if attempt == 2:
                    raise
                await asyncio.sleep(self._backoff * 2**attempt)
                continue
            if response.status_code >= 500 and attempt < 2:
                await asyncio.sleep(self._backoff * 2**attempt)
                continue
            return response
        raise RuntimeError("unreachable")

    # one chat completion returning the message object
    async def _chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": self._model, "messages": messages}
        if tools:
            payload["tools"] = tools
        response = await self._post(f"{self._base}/chat/completions", json=payload)
        response.raise_for_status()
        message: dict[str, Any] = response.json()["choices"][0]["message"]
        return message

    # return one schema validated object, replayed from fixtures when set
    async def structured(
        self, prompt: str, schema: type[T], images: Sequence[bytes] = ()
    ) -> T:
        model = cast(type[BaseModel], schema)
        if self._fixtures is not None:
            path = fixture_path(self._fixtures, prompt, images)
            if not path.exists():
                raise FileNotFoundError(path.name)
            return cast(T, model.model_validate_json(path.read_text(encoding="utf-8")))
        content: Any = prompt
        if images:
            parts: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
            for image in images:
                encoded = base64.b64encode(image).decode()
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}
                    }
                )
            content = parts
        schema_json = json.dumps(model.model_json_schema())
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": f"return only json matching this schema {schema_json}"},
            {"role": "user", "content": content}
        ]
        for attempt in range(2):
            message = await self._chat(messages)
            raw = message.get("content")
            try:
                return cast(T, model.model_validate_json(raw if isinstance(raw, str) else ""))
            except ValueError as error:
                if attempt == 1:
                    raise ValueError("model returned invalid json twice") from error
                messages = messages + [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": "invalid, return only valid json for the schema"}
                ]
        raise RuntimeError("unreachable")

    # run tools until the model answers with text
    async def run_tools(
        self, system: str, user: str, tools: Sequence[Tool]
    ) -> ToolRunResult:
        by_name = {tool.name: tool for tool in tools}
        offered = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters
                }
            }
            for tool in tools
        ]
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user}
        ]
        calls: list[str] = []
        steps = 0
        for _ in range(MAX_TOOL_ROUNDS):
            message = await self._chat(messages, offered)
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                return ToolRunResult(
                    text=str(message.get("content") or ""), steps=steps, calls=calls
                )
            messages.append(message)
            for call in tool_calls:
                function = call.get("function", {})
                name = str(function.get("name", ""))
                args = json.loads(function.get("arguments") or "{}")
                # tool args are dates, categories and counts, never user details
                LOG.info("tool %s %s", name, args)
                result = await by_name[name].run(args)
                calls.append(name)
                steps += 1
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(call.get("id", "")),
                        "content": json.dumps(result, default=str)
                    }
                )
        raise RuntimeError("tool loop did not finish")

    # transcribe one audio clip
    async def transcribe(self, audio: bytes, mime: str) -> str:
        response = await self._post(
            f"{self._base}/asr/transcribe", files={"file": ("audio", audio, mime)}
        )
        response.raise_for_status()
        return str(response.json().get("text", ""))
