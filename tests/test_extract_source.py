"""F28：select_source 时间窗/回退/占位排除/排序/limit（全离线内存 sqlite）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from feedkicker import store
from feedkicker.extract_source import _cutoff_iso, select_source

_NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _insert(
    conn,
    entry_key: str,
    *,
    feed_id: str = "量子位",
    published_at: str | None,
    first_seen: str,
    ppt_synced_at: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO articles"
        " (feed_id, entry_key, title, url, description, published_at, first_seen, pushed_at, ppt_synced_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)",
        (feed_id, entry_key, "标题-" + entry_key, "https://x/" + entry_key, "摘要", published_at, first_seen, ppt_synced_at),
    )
    conn.commit()


def test_cutoff_boundary_includes_exact_n_days() -> None:
    conn = store.connect(":memory:")
    exact = _NOW - timedelta(days=7)
    _insert(conn, "exact", published_at=_iso(exact), first_seen=_iso(_NOW))
    _insert(conn, "just-old", published_at=_iso(exact - timedelta(seconds=1)), first_seen=_iso(_NOW))
    _insert(conn, "recent", published_at=_iso(exact + timedelta(seconds=1)), first_seen=_iso(_NOW))

    got = select_source(conn, 7, now=_NOW)

    assert [r["entry_key"] for r in got] == ["exact", "recent"]
    assert got[0]["title"] == "标题-exact"
    assert got[0]["url"] == "https://x/exact"
    assert got[0]["description"] == "摘要"
    assert got[0]["published_at"] == _iso(exact)
    assert got[0]["first_seen"] == _iso(_NOW)


def test_published_at_null_falls_back_to_first_seen() -> None:
    conn = store.connect(":memory:")
    _insert(conn, "no-pub-recent", published_at=None, first_seen=_iso(_NOW - timedelta(days=1)))
    _insert(conn, "no-pub-old", published_at=None, first_seen=_iso(_NOW - timedelta(days=30)))
    _insert(conn, "pub-wins", published_at=_iso(_NOW - timedelta(days=1)), first_seen=_iso(_NOW - timedelta(days=30)))

    got = select_source(conn, 7, now=_NOW)

    assert [r["entry_key"] for r in got] == ["no-pub-recent", "pub-wins"]


def test_placeholder_rows_never_selected() -> None:
    conn = store.connect(":memory:")
    _insert(conn, "salon-placeholder", published_at=_iso(_NOW), first_seen=_iso(_NOW), ppt_synced_at=_iso(_NOW))
    _insert(conn, "rss", published_at=_iso(_NOW), first_seen=_iso(_NOW))

    got = select_source(conn, 7, now=_NOW)

    assert [r["entry_key"] for r in got] == ["rss"]


def test_order_is_time_asc_then_feed_then_entry_key() -> None:
    conn = store.connect(":memory:")
    t1, t2 = _iso(_NOW - timedelta(days=2)), _iso(_NOW - timedelta(days=1))
    _insert(conn, "b", feed_id="源B", published_at=t2, first_seen=t2)
    _insert(conn, "a", feed_id="源A", published_at=t1, first_seen=t1)
    _insert(conn, "b2", feed_id="源B", published_at=t1, first_seen=t1)
    _insert(conn, "a2", feed_id="源A", published_at=t1, first_seen=t1)

    got = select_source(conn, 7, now=_NOW)

    assert [(r["feed_id"], r["entry_key"]) for r in got] == [
        ("源A", "a"),
        ("源A", "a2"),
        ("源B", "b2"),
        ("源B", "b"),
    ]


def test_limit_applies_to_sorted_result() -> None:
    conn = store.connect(":memory:")
    for i in range(5):
        ts = _iso(_NOW - timedelta(days=5 - i))
        _insert(conn, f"k{i}", published_at=ts, first_seen=ts)

    assert [r["entry_key"] for r in select_source(conn, 7, limit=2, now=_NOW)] == ["k0", "k1"]
    assert len(select_source(conn, 7, limit=0, now=_NOW)) == 5
    assert len(select_source(conn, 7, now=_NOW)) == 5


def test_cutoff_uses_utc_iso_seconds() -> None:
    assert _cutoff_iso(7, now=_NOW) == "2026-09-07T12:00:00Z"
