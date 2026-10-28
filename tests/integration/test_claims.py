import asyncio
import uuid

from sqlalchemy import select, update

from splitledger.db.models import Bill, BillItem, BillStatus, Participant, User
from splitledger.split import claims as claims_module
from splitledger.split.claims import ClaimBook

DISHES = [("Pasta", 2000), ("Pasta", 2000), ("Calamari", 1800)]


# a dinner with two pastas and a calamari, paid by the first user
async def seed(sessions, even_ways=None) -> tuple[Bill, list[BillItem], Participant]:
    payer = User(wa_user_id="6581234567", currency="SGD", timezone="Asia/Singapore")
    async with sessions() as session:
        session.add(payer)
        await session.flush()
        bill = Bill(
            id=uuid.uuid4(), code="K7Q2AB", payer_id=payer.id, currency="SGD", subtotal=5800,
            total=7000, even_ways=even_ways
        )
        session.add(bill)
        await session.flush()
        items = [
            BillItem(id=uuid.uuid4(), bill_id=bill.id, name=name, price_cents=cents, position=n)
            for n, (name, cents) in enumerate(DISHES)
        ]
        me = Participant(id=uuid.uuid4(), bill_id=bill.id, user_id=payer.id, guest_name="Tommy")
        session.add_all([*items, me])
        await session.commit()
    return bill, items, me


# names are matched without case so the same friend never appears twice
async def test_join_matches_names(sessions):
    bill, _, _ = await seed(sessions)
    book = ClaimBook(sessions)
    anna = await book.join(bill.id, " Anna ")
    assert await book.join(bill.id, "anna") == anna
    view = await book.view(bill.code, anna)
    assert view is not None and [p.guest_name for p in view.people] == ["Tommy", "Anna"]


# mine is refused once taken, share joins it, drop lets go, and the view shows holders
async def test_claim_share_drop(sessions):
    bill, items, me = await seed(sessions)
    book = ClaimBook(sessions)
    anna = await book.join(bill.id, "Anna")
    calamari = items[2].id
    assert await book.claim(bill.id, me.id, calamari, "mine") is None
    assert await book.claim(bill.id, anna, calamari, "mine") == claims_module.TAKEN.format("Tommy")
    assert await book.claim(bill.id, anna, calamari, "share") is None
    assert await book.claim(bill.id, anna, items[0].id, "mine") is None
    view = await book.view(bill.code, anna)
    assert view is not None
    assert view.lines[2].holders == ["Tommy", "Anna"] and view.lines[2].mine
    assert view.unclaimed == 1
    # anna has a pasta and half the calamari, 2900 of 5800 so half of 7000
    assert view.share == 3500
    await book.claim(bill.id, anna, calamari, "drop")
    view = await book.view(bill.code, anna)
    assert view is not None and view.lines[2].holders == ["Tommy"]


# two people tapping the same free item at once, only one gets it
async def test_claims_dont_race(sessions):
    bill, items, _ = await seed(sessions)
    book = ClaimBook(sessions)
    anna = await book.join(bill.id, "Anna")
    ben = await book.join(bill.id, "Ben")
    pasta = items[0].id
    results = await asyncio.gather(
        book.claim(bill.id, anna, pasta, "mine"), book.claim(bill.id, ben, pasta, "mine")
    )
    assert sorted(result is None for result in results) == [False, True]
    view = await book.view(bill.code, None)
    assert view is not None and len(view.lines[0].holders) == 1


# a closed bill refuses changes, and an even split shows the payer covering odd cents
async def test_closed_and_even(sessions):
    bill, items, me = await seed(sessions, even_ways=3)
    book = ClaimBook(sessions)
    anna = await book.join(bill.id, "Anna")
    view = await book.view(bill.code, anna)
    assert view is not None and view.share == 2333
    view = await book.view(bill.code, me.id)
    assert view is not None and view.share == 2334
    async with sessions() as session:
        await session.execute(update(Bill).values(status=BillStatus.FINALISED))
        await session.commit()
    assert await book.claim(bill.id, anna, items[0].id, "mine") == claims_module.CLOSED


# ticked boxes become exactly my items, the same box ticked by two is shared
async def test_set_mine(sessions):
    bill, items, me = await seed(sessions)
    book = ClaimBook(sessions)
    anna = await book.join(bill.id, "Anna")
    assert await book.set_mine(bill.id, me.id, {items[0].id, items[2].id}) is None
    assert await book.set_mine(bill.id, anna, {items[2].id}) is None
    assert await book.set_mine(bill.id, me.id, {items[2].id}) is None
    view = await book.view(bill.code, me.id)
    assert view is not None
    assert view.lines[0].holders == [] and view.lines[2].holders == ["Tommy", "Anna"]
    assert view.lines[2].part == 900
    groups = view.groups()
    assert [[number for number, _ in group] for group in groups] == [[1, 2], [3]]


# only the payer can turn an even split into ticking items
async def test_to_items(sessions):
    bill, _, me = await seed(sessions, even_ways=3)
    book = ClaimBook(sessions)
    anna = await book.join(bill.id, "Anna")
    assert await book.to_items(bill.id, anna) == claims_module.ONLY_PAYER
    assert await book.to_items(bill.id, me.id) is None
    view = await book.view(bill.code, me.id)
    assert view is not None and view.bill.even_ways is None


# a bill from before units gets one row per serving and real letters, claimed lines stay whole
async def test_old_bill_tidied(sessions):
    bill, items, me = await seed(sessions)
    async with sessions() as session:
        session.add_all([
            BillItem(id=uuid.uuid4(), bill_id=bill.id, name="Speckru00f6sti", qty=2,
                     price_cents=4601, position=3),
            BillItem(id=uuid.uuid4(), bill_id=bill.id, name="Bier", qty=3, price_cents=2700,
                     position=4)
        ])
        await session.commit()
    book = ClaimBook(sessions)
    async with sessions() as session:
        beer = (await session.execute(select(BillItem).where(BillItem.name == "Bier"))).scalar_one()
    await book.claim(bill.id, me.id, beer.id, "mine")
    view = await book.view(bill.code, me.id)
    assert view is not None
    assert [(line.name, line.price) for line in view.lines[3:]] == [
        ("Speckrösti", 2301), ("Speckrösti", 2300), ("Bier", 2700)
    ]
    again = await book.view(bill.code, me.id)
    assert again is not None and len(again.lines) == 6
