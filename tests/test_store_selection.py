"""#196：salon 占位行（ppt_synced_at 非空、pushed_at NULL）不得进 push/archive 选择。"""

from __future__ import annotations

from feedkicker import store

NOW = "2026-09-14T00:00:00Z"


def _real_row(conn) -> None:
    store.download(
        conn,
        "F",
        [
            {
                "entry_key": "k1",
                "title": "真实资讯",
                "url": "https://e.com/1",
                "description": "",
                "published_at": None,
            }
        ],
        NOW,
    )


def _placeholder_row(conn) -> None:
    store.mark_topic_archived(
        conn, "tblSalon", "recStub000", "沙龙选题", "https://e.com/s", "摘录", NOW
    )


def test_select_pending_excludes_salon_placeholder() -> None:
    conn = store.connect(":memory:")
    _real_row(conn)
    _placeholder_row(conn)
    assert [r["entry_key"] for r in store.select_pending(conn)] == ["k1"]
    conn.close()


def test_select_unsynced_excludes_salon_placeholder() -> None:
    conn = store.connect(":memory:")
    _real_row(conn)
    _placeholder_row(conn)
    assert [r["entry_key"] for r in store.select_unsynced(conn)] == ["k1"]
    conn.close()
