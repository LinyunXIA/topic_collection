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


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def download(conn: sqlite3.Connection, feed_id: str, entries: list[dict], now_iso: str) -> int:
    rows = [
        (
            feed_id,
            e["entry_key"],
            e["title"] or "",
            e["url"],
            e["description"],
            e["published_at"],
            now_iso,
        )
        for e in entries
    ]
    if not rows:
        return 0
    try:
        cur = conn.executemany(
            "INSERT INTO articles"
            " (feed_id, entry_key, title, url, description, published_at, first_seen, pushed_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, NULL)"
            " ON CONFLICT (feed_id, entry_key) DO NOTHING",
            rows,
        )
        conn.commit()
        return max(cur.rowcount, 0)
    except Exception:
        conn.rollback()
        raise


def is_first_run(conn: sqlite3.Connection, feed_id: str) -> bool:
    row = conn.execute(
        "SELECT first_run_at FROM feeds WHERE feed_id = ?", (feed_id,)
    ).fetchone()
    return row is None or row[0] == ""


def promise_skip_old(
    conn: sqlite3.Connection, feed_id: str, cutoff_iso: str, now_iso: str
) -> int:
    cur = conn.execute(
        "UPDATE articles SET pushed_at = ?"
        " WHERE feed_id = ? AND published_at IS NOT NULL AND published_at < ? AND pushed_at IS NULL",
        (now_iso, feed_id, cutoff_iso),
    )
    conn.commit()
    return cur.rowcount


def select_pending(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT feed_id, entry_key, title, url, description, published_at"
        " FROM articles WHERE pushed_at IS NULL"
        " ORDER BY feed_id, first_seen, entry_key"
    ).fetchall()
    keys = ("feed_id", "entry_key", "title", "url", "description", "published_at")
    return [dict(zip(keys, r)) for r in rows]


def select_pushed_since(conn: sqlite3.Connection, since_iso: str) -> list[dict]:
    rows = conn.execute(
        "SELECT feed_id, entry_key, title, url, description, published_at, pushed_at"
        " FROM articles WHERE pushed_at IS NOT NULL AND pushed_at >= ?"
        " ORDER BY pushed_at, feed_id",
        (since_iso,),
    ).fetchall()
    keys = (
        "feed_id",
        "entry_key",
        "title",
        "url",
        "description",
        "published_at",
        "pushed_at",
    )
    return [dict(zip(keys, r)) for r in rows]


def mark_pushed(conn: sqlite3.Connection, items: list[dict], now_iso: str) -> None:
    conn.executemany(
        "UPDATE articles SET pushed_at = ? WHERE feed_id = ? AND entry_key = ?",
        [(now_iso, it["feed_id"], it["entry_key"]) for it in items],
    )
    conn.commit()


def bump_fail(conn: sqlite3.Connection, feed_id: str, url: str) -> None:
    conn.execute(
        "INSERT INTO feeds (feed_id, url, first_run_at, fail_streak)"
        " VALUES (?, ?, '', 1)"
        " ON CONFLICT (feed_id) DO UPDATE SET fail_streak = fail_streak + 1, url = excluded.url",
        (feed_id, url),
    )
    conn.commit()


def clear_fail(conn: sqlite3.Connection, feed_id: str) -> None:
    conn.execute("UPDATE feeds SET fail_streak = 0 WHERE feed_id = ?", (feed_id,))
    conn.commit()


def update_first_run_all(conn: sqlite3.Connection, feeds: list, now_iso: str) -> None:
    conn.executemany(
        "INSERT INTO feeds (feed_id, url, first_run_at, fail_streak)"
        " VALUES (?, ?, ?, 0)"
        " ON CONFLICT (feed_id) DO UPDATE SET"
        "   first_run_at = CASE WHEN first_run_at = '' THEN excluded.first_run_at ELSE first_run_at END,"
        "   url = excluded.url",
        [(f.name, f.url, now_iso) for f in feeds],
    )
    conn.commit()


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
