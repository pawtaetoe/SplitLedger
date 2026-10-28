import asyncio
import json
from pathlib import Path

from splitledger.handlers.log import RECEIPT_PROMPT
from splitledger.handlers.router import PROMPT
from splitledger.llm.client import MetaLLM, fixture_path
from splitledger.llm.schemas import MessageResult, ReceiptDraft, SmokeResult
from splitledger.settings import settings

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
PROMPT_FILE = ROOT / "src" / "splitledger" / "llm" / "prompts" / "smoke.md"
RECEIPTS = ROOT / "tests" / "fixtures" / "receipts"
MESSAGES = ROOT / "tests" / "fixtures" / "messages.json"


# record the smoke, receipt and labelled message fixtures from the real api
async def main() -> int:
    if not settings.meta_api_key:
        print("META_API_KEY is not set in .env")
        return 1
    prompt = f"smoke: {PROMPT_FILE.read_text(encoding='utf-8').strip()}"
    result = await MetaLLM().structured(prompt, SmokeResult)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    path = fixture_path(FIXTURES, prompt)
    path.write_text(json.dumps(result.model_dump(), indent=2), encoding="utf-8")
    print(f"wrote {path.relative_to(ROOT)}")
    for photo in sorted(RECEIPTS.glob("*.jpg")):
        image = photo.read_bytes()
        receipt = await MetaLLM().structured(RECEIPT_PROMPT, ReceiptDraft, images=[image])
        path = fixture_path(FIXTURES, RECEIPT_PROMPT, [image])
        path.write_text(json.dumps(receipt.model_dump(), indent=2), encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)} for {photo.name}")
    for row in json.loads(MESSAGES.read_text(encoding="utf-8")):
        prompt = f"read_message: {PROMPT}{row['text']}"
        result = await MetaLLM().structured(prompt, MessageResult)
        path = fixture_path(FIXTURES, prompt)
        path.write_text(json.dumps(result.model_dump(), indent=2), encoding="utf-8")
        mark = "ok" if result.intent == row["intent"] else f"got {result.intent}"
        print(f"{mark:>12}  {row['text']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
