import asyncio

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from splitledger.db.models import Base
from splitledger.settings import settings

config = context.config

target_metadata = Base.metadata


# run migrations against the async engine
async def _run_async() -> None:
    connectable = create_async_engine(settings.database_url)
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run)
    await connectable.dispose()


# configure context and run
def _do_run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


# entry point for alembic
def run_migrations() -> None:
    asyncio.run(_run_async())


run_migrations()
