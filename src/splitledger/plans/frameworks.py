from dataclasses import dataclass, field

NEEDS = ("Groceries", "Transport", "Bills", "Health")
WANTS = ("Dining", "Entertainment", "Shopping", "Travel", "Other")


# one established budget framework with where it comes from
@dataclass(frozen=True)
class Framework:
    key: str
    name: str
    blurb: str
    source: str
    buckets: dict[str, int]
    category_map: dict[str, str]
    caps: dict[str, int] = field(default_factory=dict)

    # buckets nothing is spent from, they are targets to keep
    def savings(self) -> list[str]:
        spent_from = set(self.category_map.values())
        return [name for name in self.buckets if name not in spent_from]


# map needs and wants categories onto two bucket names
def split_map(needs: str, wants: str) -> dict[str, str]:
    return {**{c: needs for c in NEEDS}, **{c: wants for c in WANTS}}


# pay yourself first with a chosen savings share
def pay_yourself_first(save_pct: int) -> Framework:
    return Framework(
        "pyf",
        "Pay yourself first",
        "save a set share, spend the rest",
        "Thaler and Benartzi, Save More Tomorrow, Journal of Political Economy (2004)",
        {"Savings": save_pct, "Spending": 100 - save_pct},
        {c: "Spending" for c in (*NEEDS, *WANTS)}
    )


PRESETS = {
    "503020": Framework(
        "503020",
        "50/30/20",
        "needs, wants, savings",
        "Warren and Warren Tyagi, All Your Worth (2005)",
        {"Needs": 50, "Wants": 30, "Savings": 20},
        split_map("Needs", "Wants")
    ),
    "60": Framework(
        "60",
        "60% solution",
        "bills first, then fun and saving",
        "Richard Jenkins, MSN Money (2004)",
        {"Committed": 60, "Fun": 10, "Irregular": 10, "Retirement": 10, "Long-term": 10},
        split_map("Committed", "Fun")
    ),
    "pyf": pay_yourself_first(20)
}
SAVE_CHOICES = (10, 15, 20)
