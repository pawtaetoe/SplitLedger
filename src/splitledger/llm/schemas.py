from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

Category = Literal[
    "Dining", "Groceries", "Transport", "Shopping", "Bills", "Entertainment", "Health",
    "Travel", "Other"
]


# currency, amount symbols and keywords one region understands
@dataclass(frozen=True)
class Locale:
    currency: str
    symbols: tuple[str, ...]
    words: dict[Category, tuple[str, ...]]

    # this locale plus a region of its own currency, symbols and extra words
    def extend(
        self, currency: str, symbols: tuple[str, ...], words: dict[Category, tuple[str, ...]]
    ) -> "Locale":
        keys = {*self.words, *words}
        merged = {key: self.words.get(key, ()) + words.get(key, ()) for key in keys}
        return Locale(currency, symbols, merged)


# common currency codes, any of these in a message lets the model pick the currency
CURRENCY_CODES = frozenset(
    {
        "usd", "eur", "gbp", "jpy", "cny", "hkd", "twd", "krw", "sgd", "myr", "idr", "thb",
        "php", "vnd", "inr", "aud", "nzd", "cad", "chf", "brl", "mxn", "aed"
    }
)


# words most people anywhere would use
GLOBAL = Locale(
    "USD",
    ("$",),
    {
        "Dining": (
            "coffee", "tea", "bubble", "breakfast", "brunch", "lunch", "dinner", "snack",
            "food", "meal", "restaurant", "cafe", "starbucks", "mcdonalds"
        ),
        "Groceries": ("groceries", "grocery", "supermarket", "costco", "walmart"),
        "Transport": (
            "uber", "taxi", "cab", "bus", "train", "metro", "subway", "petrol", "gas", "fuel",
            "parking", "toll"
        ),
        "Shopping": ("amazon", "ikea", "uniqlo", "shopping", "clothes", "shoes"),
        "Bills": (
            "bill", "bills", "rent", "phone", "electricity", "water", "internet", "wifi",
            "netflix", "spotify", "subscription"
        ),
        "Entertainment": ("movie", "movies", "cinema", "concert", "game", "games", "steam"),
        "Health": ("doctor", "clinic", "dental", "dentist", "pharmacy", "medicine", "gym"),
        "Travel": ("hotel", "hostel", "flight", "airbnb")
    }
)


# smoke test result for the fixture recorder
class SmokeResult(BaseModel):
    ok: bool
    text: str = ""


# one line on a receipt with its line total
class ReceiptItem(BaseModel):
    name: str
    qty: int = 1
    price_cents: int


# one receipt read from a photo, money in cents
class ReceiptDraft(BaseModel):
    vendor: str | None = None
    currency: str | None = None
    category: Category = "Other"
    items: list[ReceiptItem] = []
    subtotal_cents: int | None = None
    service_cents: int = 0
    tax_cents: int = 0
    discount_cents: int = 0
    total_cents: int | None = None


# one expense parsed from a message, currency none means the locale default
class ExpenseDraft(BaseModel):
    amount_cents: int | None
    currency: str | None = None
    category: Category = "Other"
    vendor: str | None = None
    note: str | None = None
