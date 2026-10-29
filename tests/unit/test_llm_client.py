import hashlib
import json
import shutil
import subprocess

import httpx
import pytest
import respx

from splitledger.contracts.llm import Tool
from splitledger.llm.audio import to_wav
from splitledger.llm.client import MetaLLM
from splitledger.llm.schemas import SmokeResult

CHAT_URL = "https://api.meta.ai/v1/chat/completions"


# build one chat completion response body
def chat_body(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


# build one tool call response body
def tool_body(name: str, arguments: dict) -> dict:
    call = {
        "id": "call1",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)}
    }
    message = {"role": "assistant", "content": None, "tool_calls": [call]}
    return {"choices": [{"message": message}]}


# replay mode loads the fixture named by prompt name and input hash
async def test_structured_replays_fixture(tmp_path):
    prompt = "smoke: say ok"
    digest = hashlib.sha256(prompt.encode()).hexdigest()[:12]
    fixture = tmp_path / f"smoke-{digest}.json"
    fixture.write_text(json.dumps({"ok": True, "text": "hi"}), encoding="utf-8")
    llm = MetaLLM(fixtures_dir=tmp_path)
    result = await llm.structured(prompt, SmokeResult)
    assert result == SmokeResult(ok=True, text="hi")


# replay mode without a fixture fails instead of calling the network
async def test_structured_replay_miss_raises(tmp_path):
    llm = MetaLLM(fixtures_dir=tmp_path)
    with pytest.raises(FileNotFoundError):
        await llm.structured("smoke: say ok", SmokeResult)


# invalid json triggers exactly one repair call
@respx.mock
async def test_structured_repairs_invalid_json_once():
    route = respx.post(CHAT_URL).mock(
        side_effect=[
            httpx.Response(200, json=chat_body("not json")),
            httpx.Response(200, json=chat_body('{"ok": true, "text": "fixed"}')),
            httpx.Response(200, json=chat_body("still not json")),
            httpx.Response(200, json=chat_body("still not json"))
        ]
    )
    llm = MetaLLM(backoff=0)
    result = await llm.structured("smoke: say ok", SmokeResult)
    assert result.text == "fixed"
    assert route.call_count == 2
    with pytest.raises(ValueError):
        await llm.structured("smoke: say ok", SmokeResult)
    assert route.call_count == 4


# a server error is retried and then succeeds
@respx.mock
async def test_chat_retries_on_server_error():
    route = respx.post(CHAT_URL).mock(
        side_effect=[
            httpx.Response(500, json={"error": "boom"}),
            httpx.Response(200, json=chat_body('{"ok": true, "text": "hi"}'))
        ]
    )
    llm = MetaLLM(backoff=0)
    result = await llm.structured("smoke: say ok", SmokeResult)
    assert result.ok is True
    assert route.call_count == 2


# the tool loop runs a requested tool then returns the final text
@respx.mock
async def test_run_tools_executes_then_answers():
    seen: list[dict] = []

    async def add(args: dict) -> dict:
        seen.append(args)
        return {"total": 3}

    respx.post(CHAT_URL).mock(
        side_effect=[
            httpx.Response(200, json=tool_body("add", {"a": 1, "b": 2})),
            httpx.Response(200, json=chat_body("the total is 3"))
        ]
    )
    llm = MetaLLM(backoff=0)
    result = await llm.run_tools("sys", "add them", [Tool("add", "add numbers", add)])
    assert seen == [{"a": 1, "b": 2}]
    assert result.text == "the total is 3"
    assert result.steps == 1
    assert result.calls == ["add"]


# transcribe posts the wav as audio with the model settings and returns the transcript
@respx.mock
async def test_transcribe_returns_text():
    route = respx.post("https://api.meta.ai/v1/asr/transcribe").mock(
        return_value=httpx.Response(200, json={"transcript": "coffee six fifty"})
    )
    llm = MetaLLM(api_key="key", backoff=0)
    text = await llm.transcribe(b"RIFF-wav-bytes", "audio/wav")
    assert text == "coffee six fifty"
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer key"
    body = request.content.decode(errors="replace")
    assert 'name="audio"; filename="voice.wav"' in body and "RIFF-wav-bytes" in body
    assert '"model": "muse-voice-transcribe-1.0"' in body and '"audioEncoding": "WAV"' in body


# an ogg voice note becomes 16 khz mono wav, a wav is left alone
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
async def test_to_wav():
    assert await to_wav(b"RIFFabc") == b"RIFFabc"
    made = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=duration=1", "-c:a",
         "libopus", "-f", "ogg", "pipe:1"],
        capture_output=True, check=True
    ).stdout
    wav = await to_wav(made)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    assert int.from_bytes(wav[22:24], "little") == 1
    assert int.from_bytes(wav[24:28], "little") == 16000
