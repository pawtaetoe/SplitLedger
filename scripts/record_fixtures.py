import asyncio
import json
from pathlib import Path

from splitledger.handlers.log import RECEIPT_PROMPT
from splitledger.llm.client import MetaLLM, fixture_path
from splitledger.llm.schemas import ReceiptDraft, SmokeResult
from splitledger.settings import settings

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
PROMPT_FILE = ROOT / "src" / "splitledger" / "llm" / "prompts" / "smoke.md"
RECEIPTS = ROOT / "tests" / "fixtures" / "receipts"


# record the smoke and receipt fixtures from the real api
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
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
