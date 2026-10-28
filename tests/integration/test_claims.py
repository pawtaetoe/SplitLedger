import asyncio
import uuid

from sqlalchemy import update

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
