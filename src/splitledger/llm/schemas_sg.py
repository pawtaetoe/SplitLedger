from splitledger.llm.schemas import GLOBAL

# singapore words on top of the global ones
SG = GLOBAL.extend(
    "SGD",
    ("$", "S$"),
    {
        "Dining": ("kopi", "teh", "makan", "hawker", "prata"),
        "Groceries": ("ntuc", "fairprice"),
        "Transport": ("mrt", "grab", "gojek", "ezlink"),
        "Shopping": ("shopee", "lazada", "qoo10"),
        "Bills": ("singtel", "starhub"),
        "Health": ("polyclinic", "guardian", "watsons")
    }
)
