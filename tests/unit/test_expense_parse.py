import pytest

from splitledger.handlers.log import clean_note, money, quick_parse
from splitledger.llm.schemas import GLOBAL
from splitledger.llm.schemas_sg import SG

GLOBAL_ROWS = [
    ("Coffee $6.50", 650, "Dining"),
    ("coffee 6.5", 650, "Dining"),
    ("$6.50 coffee", 650, "Dining"),
    ("uber to office 14.20", 1420, "Transport"),
    ("$12 lunch", 1200, "Dining"),
    ("45 gas", 4500, "Transport"),
    ("groceries 32.15", 3215, "Groceries"),
    ("Netflix 19.98", 1998, "Bills"),
    ("movie 13.50", 1350, "Entertainment"),
    ("doctor 25", 2500, "Health"),
    ("amazon 8.90", 890, "Shopping"),
    ("hotel 180", 18000, "Travel"),
    ("Bubble tea $4", 400, "Dining")
]
SG_ROWS = [
    ("kopi 1.80", 180, "Dining"),
    ("MRT 2.10", 210, "Transport"),
    ("ntuc 32.15", 3215, "Groceries"),
    ("grab to office 14.20", 1420, "Transport"),
    ("S$12 lunch", 1200, "Dining")
]
MODEL_ROWS = [
    "lunch with team 12.50 and drinks 8",
    "paid the plumber 80",
    "bought stuff",
    "€5 coffee",
    "SGD 45 petrol"
]


# global words work anywhere when the model is down
@pytest.mark.parametrize(("text", "cents", "category"), GLOBAL_ROWS)
def test_global_fast_path(text, cents, category):
    draft = quick_parse(text, GLOBAL)
    assert draft is not None
    assert (draft.amount_cents, draft.category) == (cents, category)


# singapore words work once the sg locale is on
@pytest.mark.parametrize(("text", "cents", "category"), SG_ROWS)
def test_sg_fast_path(text, cents, category):
    draft = quick_parse(text, SG)
    assert draft is not None
    assert (draft.amount_cents, draft.category) == (cents, category)


# local words are unknown to the global locale
def test_local_words_need_their_locale():
    assert quick_parse("kopi 1.80", GLOBAL) is None
    assert quick_parse("S$12 lunch", GLOBAL) is None


# messy text, foreign symbols and currency codes are left to the model
@pytest.mark.parametrize("text", MODEL_ROWS)
def test_rules_leave_these_to_the_model(text):
    assert quick_parse(text, SG) is None


# extending keeps global words and adds the local ones
def test_extend_merges_words():
    assert "coffee" in SG.words["Dining"] and "kopi" in SG.words["Dining"]
    assert SG.currency == "SGD" and GLOBAL.currency == "USD"


# money always shows the currency code
def test_money():
    assert money(650, "SGD") == "SGD 6.50"
    assert money(100000, "JPY") == "JPY 1,000.00"
    assert money(-50, "SGD") == "SGD -0.50"
    assert money(-123456, "USD") == "USD -1,234.56"


# notes keep what was bought and drop the amount, money words and stray punctuation
@pytest.mark.parametrize(("note", "clean"), [
    ("lunch with the team, about bucks", "lunch with the team"),
    ("lunch with the team, about 25 bucks", "lunch with the team"),
    ("Just spent 45 on petrol at Shell", "petrol at shell"),
    ("gift for mom 30 SGD", "gift for mom"),
    ("Coffee $6.50", "coffee"),
    ("kopi", "kopi"),
    ("about 20 bucks", None),
    (None, None)
])
def test_clean_note(note, clean):
    assert clean_note(note) == clean
