from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from splitledger.settings import settings

engine = create_async_engine(settings.database_url)
SessionFactory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    engine, expire_on_commit=False
)
