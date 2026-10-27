import json
from pathlib import Path

import pytest

from splitledger.contracts.messages import Inbound
from splitledger.handlers.log import RECEIPT_PROMPT, ExpenseLogger, receipt_adds_up
from splitledger.llm.client import MetaLLM
from splitledger.llm.schemas import GLOBAL, ReceiptDraft, ReceiptItem
from tests.fakes import FakeLLM, FakeTransport

FIXTURES = Path(__file__).parents[1] / "fixtures"
PHOTOS = sorted((FIXTURES / "receipts").glob("*.jpg"))


# a receipt with lines and charges in cents
def receipt(
    lines: list[int], subtotal: int | None, total: int | None, service: int = 0, tax: int = 0,
    discount: int = 0
) -> ReceiptDraft:
    items = [ReceiptItem(name=f"item {n}", price_cents=cents) for n, cents in enumerate(lines)]
    return ReceiptDraft(
        items=items, subtotal_cents=subtotal, service_cents=service, tax_cents=tax,
        discount_cents=discount, total_cents=total
    )


# charges on top, included tax, discounts and small rounding all add up
@pytest.mark.parametrize(
    "draft",
    [
        receipt([6000, 4000], 10000, 11990, service=1000, tax=990),
        receipt([1850, 650], 2500, 2500),
        receipt([3000, 2000], 5000, 4500, discount=500),
        receipt([1200, 800], None, 2200, service=200),
        receipt([], None, 1340),
        receipt([999, 1001], 2000, 2003)
    ]
)
def test_adds_up(draft):
    assert receipt_adds_up(draft)


# wrong lines, wrong totals and a missing total do not add up
@pytest.mark.parametrize(
    "draft",
    [
        receipt([6000, 4000], 12000, 13200),
        receipt([6000, 4000], 10000, 15000),
        receipt([6000, 4000], 10000, None)
    ]
)
def test_does_not_add_up(draft):
    assert not receipt_adds_up(draft)


# a photo message from one user
def photo(user: str = "6581234567") -> Inbound:
    return Inbound("m1", user, "image", media_ref="photo-1")


# a transport that serves one photo
def photo_transport() -> FakeTransport:
    transport = FakeTransport()
    transport.media["photo-1"] = b"jpeg-bytes"
    return transport


# lines that do not add up ask to confirm the total and save nothing yet
async def test_mismatch_asks_to_confirm():
    llm = FakeLLM({"parse_receipt": receipt([6000, 4000], 12000, 13200)})
    transport = photo_transport()
    await ExpenseLogger(llm, None, GLOBAL).log_receipt(photo(), transport)
    to, text, buttons = transport.sent_buttons[0]
    assert "USD 132.00" in text
    assert buttons[0][0].startswith("receipt:") and buttons[0][1] == "Yes, save it"
    assert llm.structured_calls == [RECEIPT_PROMPT]


# the same photo twice calls the model once
async def test_same_photo_calls_model_once():
    llm = FakeLLM({"parse_receipt": receipt([6000, 4000], 12000, 13200)})
    transport = photo_transport()
    logger = ExpenseLogger(llm, None, GLOBAL)
    await logger.log_receipt(photo(), transport)
    await logger.log_receipt(photo(), transport)
    assert len(llm.structured_calls) == 1
    assert len(transport.sent_buttons) == 2


# an unreadable total asks the user to type it
async def test_missing_total_asks_to_type():
    llm = FakeLLM({"parse_receipt": receipt([500], None, None)})
    transport = photo_transport()
    await ExpenseLogger(llm, None, GLOBAL).log_receipt(photo(), transport)
    assert "Type it instead" in transport.sent_text[0][1]


# confirming an unknown receipt saves nothing
async def test_confirm_unknown_receipt():
    transport = FakeTransport()
    await ExpenseLogger(FakeLLM(), None, GLOBAL).confirm_receipt(photo(), "missing", transport)
    assert transport.sent_text == [("6581234567", "Nothing to save")]


# each real receipt photo replays to the totals written next to it
@pytest.mark.parametrize("photo_path", PHOTOS, ids=[path.stem for path in PHOTOS])
async def test_real_receipts(photo_path):
    expected = json.loads(photo_path.with_suffix(".expected.json").read_text(encoding="utf-8"))
    llm = MetaLLM(fixtures_dir=FIXTURES / "llm")
    draft = await llm.structured(RECEIPT_PROMPT, ReceiptDraft, images=[photo_path.read_bytes()])
    assert abs((draft.total_cents or 0) - expected["total_cents"]) <= 5
    assert receipt_adds_up(draft) == expected.get("adds_up", True)
