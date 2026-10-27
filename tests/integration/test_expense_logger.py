import asyncio
import os
import subprocess
import sys

import asyncpg
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from splitledger.contracts.messages import Inbound
from splitledger.db.models import LedgerEntry, User
from splitledger.handlers.log import ExpenseLogger
from splitledger.llm.schemas import GLOBAL, ExpenseDraft
from tests.fakes import FakeLLM, FakeTransport

ADMIN_URL = "postgresql://splitledger:splitledger@localhost:5432/splitledger"
TEST_DB = "splitledger_log_test"
TEST_URL = f"postgresql+asyncpg://splitledger:splitledger@localhost:5432/{TEST_DB}"


# db reachable check
async def _reachable() -> bool:
    try:
        conn = await asyncpg.connect(ADMIN_URL)
    except Exception:
        return False
    await conn.close()
    return True


pytestmark = pytest.mark.skipif(not asyncio.run(_reachable()), reason="compose db not running")


# run one admin statement on the compose db
async def _admin(sql: str) -> None:
    conn = await asyncpg.connect(ADMIN_URL)
    await conn.execute(sql)
    await conn.close()


# fresh migrated db per test
@pytest.fixture
async def sessions():
    await _admin(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
    await _admin(f'CREATE DATABASE "{TEST_DB}"')
    env = dict(os.environ)
    env["DATABASE_URL"] = TEST_URL
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        capture_output=True,
        env=env,
        text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    engine = create_async_engine(TEST_URL)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()
    await _admin(f'DROP DATABASE IF EXISTS "{TEST_DB}"')


# one text message from a user
def text(user: str, body: str) -> Inbound:
    return Inbound(f"m-{user}-{body}", user, "text", text=body)


# count rows of one model
async def count(sessions, model) -> int:
    async with sessions() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()


# first log creates the user once and one entry per message
async def test_log_creates_user_once(sessions):
    transport = FakeTransport()
    logger = ExpenseLogger(FakeLLM(), sessions, GLOBAL)
    await logger.log(text("6581234567", "coffee 6.50"), transport)
    await logger.log(text("6581234567", "uber 14.20"), transport)
    assert await count(sessions, User) == 1
    assert await count(sessions, LedgerEntry) == 2
    to, reply, buttons = transport.sent_buttons[0]
    assert (to, reply) == ("6581234567", "Logged USD 6.50 · Dining · coffee")
    assert buttons[0][0].startswith("undo:")


# undo voids once and a second tap changes nothing
async def test_undo_voids_once(sessions):
    transport = FakeTransport()
    logger = ExpenseLogger(FakeLLM(), sessions, GLOBAL)
    await logger.log(text("6581234567", "coffee 6.50"), transport)
    entry_id = transport.sent_buttons[0][2][0][0].removeprefix("undo:")
    await logger.undo(text("6581234567", "1"), entry_id, transport)
    async with sessions() as session:
        first = (await session.execute(select(LedgerEntry.voided_at))).scalar_one()
    await logger.undo(text("6581234567", "1"), entry_id, transport)
    async with sessions() as session:
        second = (await session.execute(select(LedgerEntry.voided_at))).scalar_one()
    assert first is not None and first == second
    assert [reply for _, reply in transport.sent_text] == ["Removed", "Nothing to undo"]


# another user cannot undo this entry
async def test_undo_ignores_other_user(sessions):
    transport = FakeTransport()
    logger = ExpenseLogger(FakeLLM(), sessions, GLOBAL)
    await logger.log(text("6581234567", "coffee 6.50"), transport)
    entry_id = transport.sent_buttons[0][2][0][0].removeprefix("undo:")
    await logger.undo(text("6599999999", "1"), entry_id, transport)
    async with sessions() as session:
        voided = (await session.execute(select(LedgerEntry.voided_at))).scalar_one()
    assert voided is None
    assert transport.sent_text == [("6599999999", "Nothing to undo")]


# no amount asks for one and writes nothing
async def test_missing_amount_asks(sessions):
    transport = FakeTransport()
    llm = FakeLLM({"parse_expense": ExpenseDraft(amount_cents=None)})
    await ExpenseLogger(llm, sessions, GLOBAL).log(text("6581234567", "bought stuff"), transport)
    assert await count(sessions, LedgerEntry) == 0
    assert "How much" in transport.sent_text[0][1]
