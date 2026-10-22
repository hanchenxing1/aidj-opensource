"""Async SQLAlchemy engine / session for API and task runtime（默认本地 SQLite，支持 PostgreSQL）。"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

settings = get_settings()


def _is_sqlite(url: str) -> bool:
    return (url or "").startswith("sqlite")


class Base(DeclarativeBase):
    pass


def _postgres_pool_kwargs() -> dict:
    """Build QueuePool kwargs for Postgres."""
    return {
        "pool_pre_ping": True,
        "pool_size": max(1, int(settings.db_pool_size)),
        "max_overflow": max(0, int(settings.db_max_overflow)),
        "pool_recycle": max(60, int(settings.db_pool_recycle_sec)),
        "pool_timeout": max(5, int(settings.db_pool_timeout_sec)),
    }


_engine_kwargs: dict = {"echo": bool(settings.sql_echo)}

if _is_sqlite(settings.database_url):
    # SQLite 本地模式：数据文件自动创建（backend/data/printfilm.db）；
    # 同进程多协程共享连接需关 check_same_thread；写锁互斥避免 database is locked
    _engine_kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    _engine_kwargs["pool_size"] = 1
    _engine_kwargs["max_overflow"] = max(1, int(settings.db_pool_size) + int(settings.db_max_overflow))
else:
    _engine_kwargs.update(_postgres_pool_kwargs())

engine = create_async_engine(settings.database_url, **_engine_kwargs)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


def ensure_sqlite_parent_dir() -> None:
    """SQLite 文件父目录不存在时自动创建。"""
    if not _is_sqlite(settings.database_url):
        return
    raw = settings.database_url.split("///", 1)[-1]
    p = Path(raw.replace("\\", "/"))
    p.parent.mkdir(parents=True, exist_ok=True)


ensure_sqlite_parent_dir()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


async def init_db() -> None:
    from app import models  # noqa: F401
    from app import models_agent  # noqa: F401
    from app import models_drama  # noqa: F401
    from app import models_api  # noqa: F401
    from app import models_settings  # noqa: F401
    from app import models_tasks  # noqa: F401
    from app import models_depina  # noqa: F401
    from app import models_entitlement  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def dispose_engine() -> None:
    """Drop pooled connections so the next asyncio.run can bind a fresh loop.

    Long-running scripts that call asyncio.run repeatedly should dispose between
    runs; otherwise asyncpg/SQLAlchemy futures stay attached to a closed loop.
    """
    await engine.dispose()


def pool_status() -> dict | None:
    """Snapshot SQLAlchemy pool counters for /api/health."""
    if _is_sqlite(settings.database_url):
        return {"role": "api", "engine": "sqlite"}
    pool = engine.pool
    return {
        "role": "api",
        "size": pool.size(),
        "checked_in": pool.checkedin(),
        "checked_out": pool.checkedout(),
        "overflow": pool.overflow(),
        "max": int(settings.db_pool_size) + int(settings.db_max_overflow),
    }
