from splitledger.plans.afford import PROPERTY_TYPES, Ask, Means, Rule, Verdict, share_check

MAS = "MAS rules for Singapore"
# omv at or below this gets the higher loan limit
OMV_LIMIT_CENTS = 2_000_000


# car loans at most 70 percent of price up to 20000 omv, else 60 percent, and 7 years
def mas_car_loan(ask: Ask, means: Means) -> list[Verdict]:
    if ask.kind != "car":
        return []
    verdicts = []
    if ask.price is not None and ask.down is not None:
        cheap = ask.omv is not None and ask.omv <= OMV_LIMIT_CENTS
        min_down = 30 if cheap else 40
        note = "" if ask.omv is not None else "assumes an OMV above S$20,000, true of most cars"
        ok = ask.down * 100 >= ask.price * min_down
        verdicts.append(Verdict(f"MAS: at least {min_down}% down on a car", MAS, ok, note=note))
    if ask.term_months is not None:
        verdicts.append(Verdict("MAS: car loan of 7 years or less", MAS, ask.term_months <= 84))
    return verdicts


# all loan repayments at most 55 percent of gross pay
def mas_tdsr(ask: Ask, means: Means) -> list[Verdict]:
    if ask.kind not in ("car", "home") or not ask.monthly:
        return []
    income, on = means.pay()
    total = ask.monthly + (ask.other_debt or 0)
    return [share_check("MAS TDSR: all loans at most 55% of pay", MAS, total, 55, income, on)]


# an hdb or ec mortgage at most 30 percent of gross pay
def mas_msr(ask: Ask, means: Means) -> list[Verdict]:
    if ask.kind != "home" or ask.property_type not in ("hdb", "ec") or not ask.monthly:
        return []
    income, on = means.pay()
    rule = "MAS MSR: HDB or EC loan at most 30% of pay"
    return [share_check(rule, MAS, ask.monthly, 30, income, on)]


SG_RULES: list[Rule] = [mas_car_loan, mas_tdsr, mas_msr]

# arguments only the singapore rules read, offered to the model for +65 users
SG_FIELDS = {
    "property_type": {"type": "string", "enum": list(PROPERTY_TYPES)},
    "omv": {"type": "number", "description": "Singapore open market value of a car"}
}
