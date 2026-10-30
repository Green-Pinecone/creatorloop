from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from ..config import get_settings

_engine: AsyncEngine | None = None
_maker: async_sessionmaker[AsyncSession] | None = None


def _async_url(url: str) -> str:
    if url.startswith("sqlite:///"):
        return url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    return url


def init_engine(url: str | None = None) -> AsyncEngine:
    global _engine, _maker
    _engine = create_async_engine(_async_url(url or get_settings().database_url))
    _maker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        init_engine()
    assert _engine is not None
    return _engine


def session_maker() -> async_sessionmaker[AsyncSession]:
    if _maker is None:
        init_engine()
    assert _maker is not None
    return _maker


async def get_session() -> AsyncIterator[AsyncSession]:
    async with session_maker()() as s:
        yield s
