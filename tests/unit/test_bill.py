from datetime import UTC, datetime, timedelta

import pytest

from splitledger.db.models import Bill, BillItem
from splitledger.handlers import bill as bill_module
from splitledger.handlers.bill import even_share, even_text, forward_text, new_code, units
from splitledger.llm.schemas import ReceiptDraft, ReceiptItem
from splitledger.split.tokens import ClaimTokens

NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)


# codes use only the clear alphabet and never repeat one already taken
def test_codes():
    taken: set[str] = set()
    for _ in range(10_000):
        code = new_code(taken)
        assert len(code) == bill_module.CODE_LENGTH and set(code) <= set(bill_module.ALPHABET)
        taken.add(code)
    assert len(taken) == 10_000
    assert not set("0O1I") & set(bill_module.ALPHABET)


# a token gives back its code until it expires, and any change breaks it
def test_tokens():
    tokens = ClaimTokens("secret")
    token = tokens.make("K7Q2AB", NOW)
    assert tokens.read(token, NOW) == "K7Q2AB"
    assert tokens.read(token, NOW + timedelta(days=15)) is None
    code, expires, signature = token.split(".")
    assert tokens.read(f"K7Q2AC.{expires}.{signature}", NOW) is None
    assert tokens.read(f"{code}.{int(expires) + 86400}.{signature}", NOW) is None
    assert ClaimTokens("other").read(token, NOW) is None
    assert tokens.read("nonsense", NOW) is None


# no secret means no links, never unsigned ones
def test_tokens_need_secret():
    with pytest.raises(RuntimeError):
        ClaimTokens("").make("K7Q2AB", NOW)


# the forwardable message numbers items, shows extra charges and one link to claim in the chat
def test_forward_text():
    bill = Bill(
        code="K7Q2AB", vendor="Jumbo", currency="SGD", total=11880, service=1000, tax=880,
        discount=0
    )
    items = [
        BillItem(name="Grilled salmon", qty=1, price_cents=8000),
        BillItem(name="Beer", qty=2, price_cents=2000)
    ]
    text = forward_text(bill, items, "https://wa.me/1?text=CLAIM%20K7Q2AB")
    assert text.startswith("*Jumbo · SGD 118.80*\n")
    assert "1. Grilled salmon · SGD 80.00\n2. Beer x2 · SGD 20.00" in text
    assert "Service, tax and discounts: SGD 18.80" in text
    assert text.endswith("*Claim in WhatsApp*\nhttps://wa.me/1?text=CLAIM%20K7Q2AB")
    assert "See the bill" not in text


# friends pay the same and the payer covers the odd cents
def test_even_share():
    assert even_share(25669, 4) == (6417, 6418)
    assert even_share(1000, 4) == (250, 250)
    bill = Bill(code="K7Q2AB", vendor=None, currency="CHF", total=25669, even_ways=4)
    text = even_text(bill, "https://wa.me/1")
    assert text.startswith("*Bill · CHF 256.69*\nSplit evenly 4 ways: *CHF 64.17* each.")


# a line of several becomes one row each with the odd cents on the first, huge counts stay whole
def test_units():
    receipt = ReceiptDraft(items=[
        ReceiptItem(name="Beer", qty=3, price_cents=1000),
        ReceiptItem(name="Dumplings", qty=40, price_cents=4000),
        ReceiptItem(name="Rosti", price_cents=2850)
    ])
    assert units(receipt) == [
        ("Beer", 334), ("Beer", 333), ("Beer", 333), ("Dumplings", 4000), ("Rosti", 2850)
    ]


# escape codes from the model become real letters
def test_receipt_names_unescaped():
    item = ReceiptItem(name="Schweins Geschn Zu00fcrcherart", price_cents=2900)
    assert item.name == "Schweins Geschn Zürcherart"
    assert ReceiptDraft(vendor="Caf\\u00e9 Menu00fc").vendor == "Café Menü"
    assert ReceiptItem(name="Menu 3", price_cents=100).name == "Menu 3"
