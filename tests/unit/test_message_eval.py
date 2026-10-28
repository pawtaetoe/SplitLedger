import json
from pathlib import Path

import pytest

from splitledger.handlers.router import PROMPT
from splitledger.llm.client import MetaLLM, fixture_path
from splitledger.llm.schemas import MessageResult

FIXTURES = Path(__file__).parents[1] / "fixtures"
ROWS = json.loads((FIXTURES / "messages.json").read_text(encoding="utf-8"))


# each labelled message replays the recorded model answer and must match its label
@pytest.mark.parametrize("row", ROWS, ids=[row["text"] for row in ROWS])
async def test_labelled_message(row):
    prompt = f"read_message: {PROMPT}{row['text']}"
    if not fixture_path(FIXTURES / "llm", prompt).exists():
        pytest.skip("not recorded yet, run make record-fixtures")
    result = await MetaLLM(fixtures_dir=FIXTURES / "llm").structured(prompt, MessageResult)
    assert result.intent == row["intent"]
    if "amount_cents" in row:
        assert result.expense is not None
        assert result.expense.amount_cents == row["amount_cents"]
