"""跨方言的「按日历日分桶」表达式（本地 SQLite / 生产 PostgreSQL）。"""

from __future__ import annotations

from sqlalchemy import Date, cast, func

from app.models import UsageEvent


def _is_sqlite() -> bool:
    from app.config import get_settings

    url = (get_settings().database_url or "").lower()
    return url.startswith("sqlite")


def usage_day_expr():
    """按日历日分桶 UsageEvent.created_at。

    PostgreSQL 用 CAST(created_at AS DATE)；SQLite 用 date(created_at)，
    返回 'YYYY-MM-DD' 字符串。下游 _day_key / str(row.day)[:10] 均兼容两种类型。
    """
    if _is_sqlite():
        return func.date(UsageEvent.created_at)
    return cast(UsageEvent.created_at, Date)
