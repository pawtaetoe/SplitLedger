import argparse
import asyncio
import uuid
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from splitledger.db import models
from splitledger.db.models import LedgerEntry, LedgerKind, LedgerSource, LedgerStatus, User
from splitledger.db.session import SessionFactory
from splitledger.handlers.router import LOCALES
from splitledger.settings import settings

# fixed namespace so each seeded row always gets the same id
NAMESPACE = uuid.UUID("5b1e4d3c-9a7f-4e2b-8c61-0d2f6a9e4b17")
# days ago, local hour, cents, category, vendor, note, voided
ROWS = [
    (0, 8, 180, "Dining", None, "coffee", False),
    (0, 13, 1250, "Dining", None, "chicken rice and drink", False),
    (1, 9, 1420, "Transport", "Grab", "ride to office", False),
    (1, 19, 8240, "Groceries", "FairPrice", "groceries", False),
    (2, 12, 960, "Dining", None, "lunch", False),
    (3, 14, 9900, "Shopping", None, "shoes", True),
    (3, 20, 4500, "Dining", None, "dinner with friends", False),
    (4, 18, 1599, "Bills", "Netflix", "subscription", False),
    (5, 15, 12900, "Shopping", "Uniqlo", "jacket", False),
    (6, 21, 2400, "Entertainment", None, "movie", False),
    (7, 10, 3500, "Health", None, "clinic visit", False),
    (8, 8, 210, "Transport", None, "train", False),
    (9, 13, 1100, "Dining", None, "lunch", False),
    (10, 19, 6380, "Groceries", None, "groceries", False),
    (12, 20, 18000, "Travel", None, "hotel deposit", False),
    (13, 12, 850, "Dining", None, "bubble tea and snack", False)
]
# label, cents, every, days from today, category
PLANNED = [
    ("rent", 180000, "month", 3, "Bills"),
    ("phone", 4800, "month", 2, "Bills"),
    ("gym", 2500, "week", 1, "Health"),
    ("mom's birthday present", 15000, "once", 20, "Shopping"),
    ("insurance", 120000, "year", 40, "Bills")
]
# planned rows get ids after the expense rows
PLANNED_FROM = 1000


# stable id for one seeded row of one user
def seed_id(wa_user_id: str, index: int) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"{wa_user_id}:{index}")


# local time for a row, pulled back to the last half hour when it would be in the future
def when(days_ago: int, hour: int, zone: ZoneInfo, now: datetime) -> datetime:
    moment = datetime.combine(now.date() - timedelta(days=days_ago), time(hour), zone)
    return min(moment, now - timedelta(minutes=30))


# add the seed rows for one user, rows already there are left alone
async def seed(wa_user_id: str, reset: bool) -> None:
    locale = LOCALES[settings.locale]
    ids = [seed_id(wa_user_id, index) for index in range(len(ROWS))]
    plan_ids = [seed_id(wa_user_id, PLANNED_FROM + index) for index in range(len(PLANNED))]
    async with SessionFactory() as session:
        if reset:
            await session.execute(delete(LedgerEntry).where(LedgerEntry.id.in_(ids)))
            planned_rows = models.PlannedSpend.id.in_(plan_ids)
            await session.execute(delete(models.PlannedSpend).where(planned_rows))
        await session.execute(
            insert(User)
            .values(wa_user_id=wa_user_id, currency=locale.currency, timezone=locale.timezone)
            .on_conflict_do_nothing(index_elements=["wa_user_id"])
        )
        query = select(User).where(User.wa_user_id == wa_user_id)
        user = (await session.execute(query)).scalar_one()
        zone = ZoneInfo(user.timezone)
        now = datetime.now(zone)
        values = [
            {
                "id": ids[index],
                "user_id": user.id,
                "kind": LedgerKind.EXPENSE,
                "amount_cents": cents,
                "currency": user.currency,
                "category": category,
                "vendor": vendor,
                "note": note,
                "source": LedgerSource.TEXT,
                "status": LedgerStatus.POSTED,
                "occurred_at": when(days_ago, hour, zone, now),
                "voided_at": now if voided else None
            }
            for index, (days_ago, hour, cents, category, vendor, note, voided) in enumerate(ROWS)
        ]
        added = await session.execute(
            insert(LedgerEntry)
            .values(values)
            .on_conflict_do_nothing(index_elements=["id"])
            .returning(LedgerEntry.id)
        )
        count = len(added.all())
        planned = [
            {
                "id": plan_ids[index], "user_id": user.id, "label": label, "amount_cents": cents,
                "currency": user.currency, "category": category, "every": every,
                "due_on": now.date() + timedelta(days=ahead)
            }
            for index, (label, cents, every, ahead, category) in enumerate(PLANNED)
        ]
        await session.execute(
            insert(models.PlannedSpend)
            .values(planned)
            .on_conflict_do_nothing(index_elements=["id"])
        )
        await session.commit()
    print(f"added {count} of {len(ROWS)} seed entries in {user.currency}, {user.timezone}")
    if count == 0:
        print("already seeded, run with --reset to replace them with fresh dates")


# parse arguments and refuse to run outside dev
def main() -> int:
    parser = argparse.ArgumentParser(description="seed two weeks of spending for one user")
    parser.add_argument("wa_user_id", help="whatsapp id, digits only, for example 6591234567")
    parser.add_argument("--reset", action="store_true", help="delete earlier seed rows first")
    args = parser.parse_args()
    if settings.env != "dev":
        print("seed_dev only runs when ENV=dev")
        return 1
    if not args.wa_user_id.isdigit():
        print("use digits only, no plus sign or spaces")
        return 1
    asyncio.run(seed(args.wa_user_id, args.reset))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
