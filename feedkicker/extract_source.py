"""F28 数据源与时间窗：sqlite 近 N 天 RSS 行选源（DESIGN §25.1/#248）。"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"

_COLUMNS = ("feed_id", "entry_key", "title", "url", "description", "published_at", "first_seen")


def _cutoff_iso(since_days: int, now: datetime | None = None) -> str:
    """截止时刻 = now − since_days 天；ISO 秒级与库内格式一致（#248）。"""
    base = now or datetime.now(UTC)
    return (base - timedelta(days=since_days)).strftime(_ISO_FMT)


def select_source(
    conn: sqlite3.Connection,
    since_days: int,
    limit: int | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """选近 N 天 RSS 资讯行，时间升序 + feed_id/entry_key 稳定排序。

    时间窗用 `COALESCE(published_at, first_seen) >= cutoff`：published_at 优先、
    为空回退 first_seen，边界含当天（>=）。占位行必有 ppt_synced_at（#196），
    故 `ppt_synced_at IS NULL` 即「仅 RSS 行」。limit 为 None/<=0 时不限制。
    """
    sql = (
        "SELECT feed_id, entry_key, title, url, description, published_at, first_seen"
        " FROM articles WHERE ppt_synced_at IS NULL"
        " AND COALESCE(published_at, first_seen) >= ?"
        " ORDER BY COALESCE(published_at, first_seen) ASC, feed_id, entry_key"
    )
    params: list[Any] = [_cutoff_iso(since_days, now)]
    if limit is not None and limit > 0:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [dict(zip(_COLUMNS, r)) for r in rows]
