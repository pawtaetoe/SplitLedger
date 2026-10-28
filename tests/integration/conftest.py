import os
import subprocess
import sys

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

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



# run one admin statement on the compose db
async def _admin(sql: str) -> None:
    conn = await asyncpg.connect(ADMIN_URL)
    await conn.execute(sql)
    await conn.close()


# fresh migrated db per test, skipped when the compose db is down
@pytest.fixture
async def sessions():
    if not await _reachable():
        pytest.skip("compose db not running")
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
