import pytest
from hypothesis import given
from hypothesis import strategies as st

from splitledger.split.engine import Bill, BillSplitter, Claim, Item, UnclaimedItems


# worked example from the main plan, 120 dollar dinner split four ways
def test_worked_example_120_dinner():
    bill = Bill(
        items=(Item(id="platter", price_cents=10000),),
        service_cents=1000,
        tax_cents=1000,
        discount_cents=0,
        total_cents=12000
    )
    claims = [
        Claim(item_id="platter", participant="alice"),
        Claim(item_id="platter", participant="bob"),
        Claim(item_id="platter", participant="carol"),
        Claim(item_id="platter", participant="dave")
    ]
    result = BillSplitter().allocate(bill, claims)
    assert result == {"alice": 3000, "bob": 3000, "carol": 3000, "dave": 3000}


# shared item splits by weight and extras spread pro rata
def test_weighted_shared_item():
    bill = Bill(
        items=(
            Item(id="laksa", price_cents=1000),
            Item(id="beer", price_cents=900)
        ),
        service_cents=190,
        tax_cents=0,
        discount_cents=0,
        total_cents=2090
    )
    claims = [
        Claim(item_id="laksa", participant="alice"),
        Claim(item_id="beer", participant="alice", weight=0.5),
        Claim(item_id="beer", participant="bob", weight=0.5)
    ]
    result = BillSplitter().allocate(bill, claims)
    assert result == {"alice": 1595, "bob": 495}
    assert sum(result.values()) == 2090


# an item nobody claimed is an error
def test_unclaimed_item_raises():
    bill = Bill(
        items=(Item(id="rice", price_cents=200),),
        service_cents=0,
        tax_cents=0,
        discount_cents=0,
        total_cents=200
    )
    with pytest.raises(UnclaimedItems):
        BillSplitter().allocate(bill, [])


people = st.sampled_from(["alice", "bob", "carol"])


# build one bill where every item is claimed
@st.composite
def bills_and_claims(draw: st.DrawFn) -> tuple[Bill, list[Claim]]:
    item_count = draw(st.integers(min_value=1, max_value=5))
    items = []
    claims: list[Claim] = []
    subtotal = 0
    for number in range(item_count):
        price = draw(st.integers(min_value=0, max_value=5000))
        item_id = f"item-{number}"
        items.append(Item(id=item_id, price_cents=price))
        subtotal += price
        claimants = draw(st.lists(people, min_size=1, max_size=3, unique=True))
        for person in claimants:
            weight = draw(st.sampled_from([0.5, 1.0, 2.0]))
            claims.append(Claim(item_id=item_id, participant=person, weight=weight))
    service = draw(st.integers(min_value=0, max_value=1000))
    tax = draw(st.integers(min_value=0, max_value=1000))
    discount = draw(st.integers(min_value=0, max_value=subtotal))
    bill = Bill(
        items=tuple(items),
        service_cents=service,
        tax_cents=tax,
        discount_cents=discount,
        total_cents=subtotal + service + tax - discount
    )
    return bill, claims


# for any claimed bill the shares sum to the total and none is negative
@given(bills_and_claims())
def test_shares_always_sum_to_total(case: tuple[Bill, list[Claim]]):
    bill, claims = case
    result = BillSplitter().allocate(bill, claims)
    assert sum(result.values()) == bill.total_cents
    assert all(amount >= 0 for amount in result.values())
