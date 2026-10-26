from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction

from splitledger.contracts.money import Cents


# one bill line, price is the line total in cents
@dataclass(frozen=True)
class Item:
    id: str
    price_cents: Cents


# one bill to split, money in cents
@dataclass(frozen=True)
class Bill:
    items: tuple[Item, ...]
    service_cents: Cents
    tax_cents: Cents
    discount_cents: Cents
    total_cents: Cents


# one participant claim on one item
@dataclass(frozen=True)
class Claim:
    item_id: str
    participant: str
    weight: float = 1.0


# raised when an item has no claims
class UnclaimedItems(Exception):
    # keep the offending item ids
    def __init__(self, item_ids: tuple[str, ...]) -> None:
        super().__init__(f"unclaimed items: {', '.join(item_ids)}")
        self.item_ids = item_ids


# splits a bill into per participant cents
class BillSplitter:
    # allocate the bill total across participants by claim weights
    def allocate(self, bill: Bill, claims: Sequence[Claim]) -> dict[str, Cents]:
        by_item = self._by_item(bill, claims)
        subtotals = self._subtotals(bill, by_item)
        exact = self._exact_shares(bill, subtotals)
        return self._round_largest_remainder(bill.total_cents, exact)

    # group claims by item and reject unknown or unclaimed items
    def _by_item(
        self, bill: Bill, claims: Sequence[Claim]
    ) -> dict[str, list[Claim]]:
        known = {item.id for item in bill.items}
        grouped: dict[str, list[Claim]] = {item.id: [] for item in bill.items}
        for claim in claims:
            if claim.item_id not in known:
                raise ValueError(f"unknown item: {claim.item_id}")
            if claim.weight <= 0:
                raise ValueError(f"claim weight must be positive: {claim.item_id}")
            grouped[claim.item_id].append(claim)
        unclaimed = tuple(item.id for item in bill.items if not grouped[item.id])
        if unclaimed:
            raise UnclaimedItems(unclaimed)
        return grouped

    # exact per participant item subtotals before extras
    def _subtotals(
        self, bill: Bill, by_item: dict[str, list[Claim]]
    ) -> dict[str, Fraction]:
        subtotals: dict[str, Fraction] = {}
        for item in bill.items:
            item_claims = by_item[item.id]
            total_weight = sum(Fraction(str(claim.weight)) for claim in item_claims)
            for claim in item_claims:
                share = Fraction(item.price_cents) * Fraction(str(claim.weight)) / total_weight
                subtotals[claim.participant] = subtotals.get(claim.participant, Fraction(0)) + share
        return subtotals

    # spread service tax and discount pro rata to subtotals
    def _exact_shares(
        self, bill: Bill, subtotals: dict[str, Fraction]
    ) -> dict[str, Fraction]:
        total = Fraction(bill.total_cents)
        base = sum(subtotals.values(), Fraction(0))
        if base == 0:
            # no priced items, split the total evenly
            if not subtotals:
                if total == 0:
                    return {}
                raise ValueError("no participants to split the total")
            each = total / len(subtotals)
            return {person: each for person in subtotals}
        return {person: share * total / base for person, share in subtotals.items()}

    # floor each share then hand leftover cents to largest remainders
    def _round_largest_remainder(
        self, total_cents: Cents, exact: dict[str, Fraction]
    ) -> dict[str, Cents]:
        floors = {person: share.numerator // share.denominator for person, share in exact.items()}
        leftover = int(total_cents) - sum(floors.values())
        order = sorted(exact, key=lambda person: exact[person] - floors[person], reverse=True)
        result = dict(floors)
        for person in order[:leftover]:
            result[person] += 1
        return {person: Cents(amount) for person, amount in result.items()}
