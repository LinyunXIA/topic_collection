"""sqlite meta 键值表：跨运行持久化的单行状态（连败计数、ppt last_status、巡检时间戳）。

独立叶子模块：store 与 store_salon 都依赖它，避免 store ↔ store_salon 导入环。
"""

from __future__ import annotations

import sqlite3


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?)"
        " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
