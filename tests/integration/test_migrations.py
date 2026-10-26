import asyncio
import os
import subprocess
import sys

import asyncpg
import pytest

ADMIN_URL = "postgresql://splitledger:splitledger@localhost:5432/splitledger"
TEST_DB = "splitledger_mig_test"
TEST_URL = f"postgresql+asyncpg://splitledger:splitledger@localhost:5432/{TEST_DB}"


# db reachable check
async def _reachable() -> bool:
    try:
        conn = await asyncpg.connect(ADMIN_URL)
    except Exception:
        return False
    await conn.close()
    return True


# run one alembic command against the throwaway db
def _alembic(*args: str) -> None:
    env = dict(os.environ)
    env["DATABASE_URL"] = TEST_URL
    env["PATH"] = os.environ["PATH"]
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        capture_output=True,
        env=env,
        text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr


# migrate up then down then up on a throwaway db
@pytest.mark.skipif(not asyncio.run(_reachable()), reason="compose db not running")
def test_migrate_up_down_up():
    async def reset() -> None:
        conn = await asyncpg.connect(ADMIN_URL)
        await conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
        await conn.execute(f'CREATE DATABASE "{TEST_DB}"')
        await conn.close()

    async def drop() -> None:
        conn = await asyncpg.connect(ADMIN_URL)
        await conn.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
        await conn.close()

    asyncio.run(reset())
    try:
        _alembic("upgrade", "head")
        _alembic("downgrade", "base")
        _alembic("upgrade", "head")
    finally:
        asyncio.run(drop())
