"""salon 选题在 sqlite 侧的状态：ppt 同步标记、last_status、落库。

meta 读写走叶子模块 store_meta，依赖方向 store_meta ← store_salon ← store(facade)，
无导入环；store.get_ppt_last_status 等调用点由 store 顶部 facade re-export 保持。
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from feedkicker.store_meta import get_meta, set_meta

log = logging.getLogger(__name__)


def is_ppt_synced(conn: sqlite3.Connection, record_id: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM articles WHERE entry_key = ? AND ppt_synced_at IS NOT NULL",
            (record_id,),
        ).fetchone()
        return row is not None
    except Exception:  # noqa: BLE001
        return False


def mark_ppt_synced(
    conn: sqlite3.Connection, items: list[dict[str, Any]] | list[str], now_iso: str
) -> None:
    if not items:
        return
    if isinstance(items[0], dict):
        conn.executemany(
            "UPDATE articles SET ppt_synced_at = ? WHERE feed_id = ? AND entry_key = ?",
            [
                (now_iso, it["feed_id"], it["entry_key"])
                for it in items
                if isinstance(it, dict)
            ],
        )
    else:
        conn.executemany(
            "UPDATE articles SET ppt_synced_at = ? WHERE entry_key = ?",
            [(now_iso, rid) for rid in items if isinstance(rid, str)],
        )
    conn.commit()


def get_ppt_last_status(conn: sqlite3.Connection, record_id: str) -> str:
    return get_meta(conn, f"ppt_last_status_{record_id}", "")


def set_ppt_last_status(conn: sqlite3.Connection, record_id: str, status: str) -> None:
    set_meta(conn, f"ppt_last_status_{record_id}", status)


def mark_topic_archived(
    conn: sqlite3.Connection,
    feed_id: str,
    record_id: str,
    title: str,
    url: str,
    md_excerpt: str,
    now_iso: str,
) -> None:
    """选题 Wiki 建成后落库：插占位 article 行、标记 ppt 已同步、last_status=已选题。

    三段写入各自独立 try/except：单段失败只 WARNING，不影响已建成的 Wiki 与卡片推送。
    """
    try:
        exists = conn.execute(
            "SELECT 1 FROM articles WHERE entry_key = ?", (record_id,)
        ).fetchone()
        if exists is None:
            conn.execute(
                "INSERT INTO articles (feed_id, entry_key, title, url, description, "
                "published_at, first_seen, pushed_at, ppt_synced_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL) "
                "ON CONFLICT (feed_id, entry_key) DO NOTHING",
                (feed_id, record_id, title, url, md_excerpt[:500], None, now_iso),
            )
            conn.commit()
    except Exception as e:  # noqa: BLE001
        log.warning("插入占位 article 失败 %s: %s", record_id, e)

    try:
        mark_ppt_synced(conn, [record_id], now_iso)
    except Exception as e:  # noqa: BLE001
        log.warning("mark_ppt_synced 失败 %s: %s", record_id, e)

    try:
        set_ppt_last_status(conn, record_id, "已选题")
    except Exception as e:  # noqa: BLE001
        log.warning("set_ppt_last_status 失败 %s: %s", record_id, e)
