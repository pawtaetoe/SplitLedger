import sqlalchemy as sa

from splitledger.db.models import Base


# all tables needed by later cards exist
def test_expected_tables_exist():
    assert set(Base.metadata.tables) == {
        "users",
        "ledger_entries",
        "processed_messages",
        "budgets",
        "bills",
        "bill_items",
        "participants",
        "item_claims",
        "allocations"
    }


# ledger amounts are bigint cents with the required index
def test_ledger_shape():
    table = Base.metadata.tables["ledger_entries"]
    assert isinstance(table.c.amount_cents.type, sa.BigInteger)
    index_columns = {
        tuple(index.columns.keys()) for index in table.indexes
    }
    assert ("user_id", "kind", "occurred_at") in index_columns


# bill money columns are bigint cents
def test_bill_amounts_are_bigint():
    table = Base.metadata.tables["bills"]
    names = ("subtotal", "service", "tax", "discount", "total")
    for name in names:
        assert isinstance(table.c[name].type, sa.BigInteger)
