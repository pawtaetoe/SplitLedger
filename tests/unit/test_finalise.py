import uuid

import pytest

from splitledger.db.models import Bill, BillItem, ItemClaim, Participant
from splitledger.split.finalise import Leftovers, even_split, item_split

PAYER = Participant(id=uuid.uuid4(), guest_name="Tommy")
ANNA = Participant(id=uuid.uuid4(), guest_name="Anna")
BEN = Participant(id=uuid.uuid4(), guest_name="Ben")
PEOPLE = [PAYER, ANNA, BEN]
ITEMS = [
    BillItem(id=uuid.uuid4(), name="Pasta", price_cents=2000),
    BillItem(id=uuid.uuid4(), name="Pasta", price_cents=2000),
    BillItem(id=uuid.uuid4(), name="Calamari", price_cents=1800)
]
BILL = Bill(subtotal=5800, service=0, tax=1200, discount=0, total=7000)


# one claim of one item
def took(person: Participant, item: BillItem) -> ItemClaim:
    return ItemClaim(bill_item_id=item.id, participant_id=person.id, weight=1.0)


# shares follow the claims and always add up to the total
def test_item_split():
    claims = [took(PAYER, ITEMS[0]), took(ANNA, ITEMS[1]), took(BEN, ITEMS[2])]
    shares = item_split(BILL, ITEMS, claims, PEOPLE, PAYER, "ask")
    assert shares == {PAYER.id: 2414, ANNA.id: 2414, BEN.id: 2172}


# unclaimed items are asked about, then shared by everyone or kept by the payer
def test_leftovers():
    claims = [took(PAYER, ITEMS[0]), took(ANNA, ITEMS[1])]
    with pytest.raises(Leftovers) as left:
        item_split(BILL, ITEMS, claims, PEOPLE, PAYER, "ask")
    assert left.value.names == ["Calamari"]
    mine = item_split(BILL, ITEMS, claims, PEOPLE, PAYER, "mine")
    assert BEN.id not in mine
    assert mine[PAYER.id] == 4586
    even = item_split(BILL, ITEMS, claims, PEOPLE, PAYER, "even")
    assert sum(even.values()) == 7000 and even[BEN.id] == 724


# an even split charges friends the same and the payer covers odd cents and no shows
def test_even_split():
    bill = Bill(total=7000, even_ways=4)
    shares = even_split(bill, PEOPLE, PAYER)
    assert shares == {ANNA.id: 1750, BEN.id: 1750, PAYER.id: 3500}
