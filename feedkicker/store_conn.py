"""sqlite 连接与 schema 迁移（自 store.py 抽出，DESIGN §21.2）。

WAL + busy_timeout 让 launchd 与人工并发（或升级首跑双进程）少撞 lock；
ALTER 迁移容忍双进程竞态下的 duplicate column（busy_timeout 只是等待，
幂等语义不变）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
  feed_id      TEXT NOT NULL,
  entry_key    TEXT NOT NULL,
  title        TEXT NOT NULL,
  url          TEXT NOT NULL,
  description  TEXT,
  published_at TEXT,
  first_seen   TEXT NOT NULL,
  pushed_at    TEXT,
  PRIMARY KEY (feed_id, entry_key)
);

CREATE TABLE IF NOT EXISTS feeds (
  feed_id      TEXT PRIMARY KEY,
  url          TEXT NOT NULL,
  first_run_at TEXT NOT NULL,
  fail_streak  INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_articles_pending ON articles (pushed_at)
  WHERE pushed_at IS NULL;

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

_MIGRATIONS = (("bitable_synced_at", "TEXT"), ("ppt_synced_at", "TEXT"))


def _add_columns(conn: sqlite3.Connection, cols: set[str]) -> None:
    """按 PRAGMA 结果补迁移列；竞态下 duplicate column 视为已补齐（#239）。"""
    for col, typ in _MIGRATIONS:
        if col in cols:
            continue
        try:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(articles)")}
    _add_columns(conn, cols)
    conn.commit()
    return conn
