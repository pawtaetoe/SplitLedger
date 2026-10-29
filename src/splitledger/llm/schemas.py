import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field, field_validator

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
    timezone: str = "UTC"

    # this locale plus a region of its own currency, symbols, extra words and timezone
    def extend(
        self, currency: str, symbols: tuple[str, ...], words: dict[Category, tuple[str, ...]],
        timezone: str
    ) -> "Locale":
        keys = {*self.words, *words}
        merged = {key: self.words.get(key, ()) + words.get(key, ()) for key in keys}
        return Locale(currency, symbols, merged, timezone)


# the value for the longest country calling code a whatsapp number starts with, else fallback
def by_country_code[T](wa_user_id: str, table: Mapping[str, T], fallback: T) -> T:
    digits = re.sub(r"\D", "", wa_user_id)
    for size in (3, 2, 1):
        if digits[:size] in table:
            return table[digits[:size]]
    return fallback


# the default region and the packs picked by a number's country calling code
@dataclass(frozen=True)
class Regions:
    default: Locale
    packs: dict[str, Locale] = field(default_factory=dict)

    # the region for one whatsapp number
    def for_user(self, wa_user_id: str) -> Locale:
        return by_country_code(wa_user_id, self.packs, self.default)


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


# one part of a plan the user described, a share of pay or a money limit
class BucketDraft(BaseModel):
    name: str
    pct: int | None = None
    cap_cents: int | None = None
    cap_per: Literal["week", "month"] | None = None
    categories: list[str] = []


# a plan drafted from the user's own words
class CustomPlan(BaseModel):
    buckets: list[BucketDraft]
    period: Literal["weekly", "monthly"] | None = None


# smoke test result for the fixture recorder
class SmokeResult(BaseModel):
    ok: bool
    text: str = ""


# escape codes like u00fc a model sometimes writes instead of the letter
ESCAPED = re.compile(r"\\?u(00[0-9a-fA-F]{2}|20[0-9a-fA-F]{2})")


# text with leftover escape codes turned back into letters
def unescape(text: str | None) -> str | None:
    return ESCAPED.sub(lambda m: chr(int(m.group(1), 16)), text) if text else text


# one line on a receipt with its line total
class ReceiptItem(BaseModel):
    name: str
    qty: int = 1
    price_cents: int

    # zürcher not zu00fcrcher
    @field_validator("name")
    @classmethod
    def letters(cls, name: str) -> str:
        return unescape(name) or name


# one receipt read from a photo, money in cents
class ReceiptDraft(BaseModel):
    vendor: str | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    category: Category = "Other"
    items: list[ReceiptItem] = []
    subtotal_cents: int | None = None
    service_cents: int = 0
    tax_cents: int = 0
    discount_cents: int = 0
    total_cents: int | None = None

    # the shop name with real letters too
    @field_validator("vendor")
    @classmethod
    def letters(cls, vendor: str | None) -> str | None:
        return unescape(vendor)


# one expense parsed from a message, currency none means the locale default
class ExpenseDraft(BaseModel):
    amount_cents: int | None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    category: Category = "Other"
    vendor: str | None = None
    note: str | None = None


# one item someone had, portion is the part they had when they say so
class ClaimPart(BaseModel):
    item: int
    portion: float | None = None


# the items one person had, name none means the sender
class PersonClaims(BaseModel):
    name: str | None = None
    items: list[ClaimPart] = []


# who had what on a bill, read from a chat message
class ClaimMap(BaseModel):
    people: list[PersonClaims] = []


# what a free text message wants plus the expense when it is one
class MessageResult(BaseModel):
    intent: Literal["log", "ask", "upcoming", "plan", "split", "claim", "chat", "unclear"]
    expense: ExpenseDraft | None = None
    # people sharing evenly when a split says how many
    ways: int | None = None
    # how a split should be shared, unsure when the message doesn't say
    split_mode: Literal["even", "items", "unsure"] | None = None
