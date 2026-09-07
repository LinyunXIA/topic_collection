"""tc-purge 365 天滚动保留测试（#120）。

全部 lark-cli 子进程经 monkeypatch bitable._run 拦截，无真实网络/子进程；
sqlite 用 :memory:，不触碰任何真实库。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from feedkicker import bitable, bitable_purge, purge, store
from feedkicker.config import (
    BitableConf,
    Config,
    HttpConf,
    MinimaxConf,
    SalonConf,
    SiteConf,
    WikiConf,
)

NOW = datetime(2026, 9, 7, 2, 0, tzinfo=UTC)
OLD_EPOCH_MS = "1735689600000"
OLD_ISO = "2025-01-15T10:00:00+08:00"
RECENT_ISO = "2026-09-01T10:00:00+08:00"


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def make_conn():
    return store.connect(":memory:")


def make_cfg(**bt_kw) -> Config:
    cfg = Config(app_env="test")
    cfg.http = HttpConf()
    cfg.site = SiteConf()
    cfg.bitable = BitableConf(**bt_kw)
    cfg.salon = SalonConf()
    cfg.minimax = MinimaxConf()
    cfg.wiki = WikiConf()
    return cfg


def add_article(conn, key, pushed_at, synced_at=None):
    conn.execute(
        "INSERT INTO articles (feed_id, entry_key, title, url, description,"
        " published_at, first_seen, pushed_at, bitable_synced_at)"
        " VALUES ('F', ?, ?, ?, '', ?, ?, ?, ?)",
        (key, key, f"https://x/{key}", pushed_at, pushed_at, pushed_at, synced_at),
    )
    conn.commit()


def rec(rid, pushed):
    return {"record_id": rid, "fields": {"推送时间": pushed}}


def page(records):
    return json.dumps({"ok": True, "data": {"records": records}}, ensure_ascii=False)


def install_fake_lark(monkeypatch, pages, delete_fail=False):
    calls: list[list[str]] = []
    deletes: list[dict] = []

    def fake_run(args, stdin_text=None, timeout=120):
        calls.append(list(args))
        if "+record-list" in args:
            idx = sum(1 for c in calls if "+record-list" in c)
            return FakeProc(0, stdout=pages[idx - 1])
        if "+record-delete" in args:
            if delete_fail:
                return FakeProc(1, stderr="boom")
            deletes.append(json.loads(args[args.index("--json") + 1]))
            return FakeProc(0, stdout="{}")
        return FakeProc(0, stdout="{}")

    monkeypatch.setattr(bitable, "_run", fake_run)
    return calls, deletes


# ── sqlite 侧 ──


def test_purge_sqlite_dry_run_counts_and_keeps():
    conn = make_conn()
    add_article(conn, "old-arch", "2025-01-01T00:00:00Z", "2025-01-02T00:00:00Z")
    add_article(conn, "old-unarch", "2025-01-03T00:00:00Z", None)
    add_article(conn, "recent", "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z")

    deleted, unarchived = purge.purge_sqlite(conn, "2025-09-07T02:00:00Z", dry_run=True)
    assert (deleted, unarchived) == (0, 1)
    assert conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0] == 3


def test_purge_sqlite_apply_deletes_only_archived():
    conn = make_conn()
    add_article(conn, "old-arch", "2025-01-01T00:00:00Z", "2025-01-02T00:00:00Z")
    add_article(conn, "old-unarch", "2025-01-03T00:00:00Z", None)
    add_article(conn, "recent", "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z")

    deleted, unarchived = purge.purge_sqlite(conn, "2025-09-07T02:00:00Z", dry_run=False)
    assert (deleted, unarchived) == (1, 1)
    left = {r[0] for r in conn.execute("SELECT entry_key FROM articles")}
    assert left == {"old-unarch", "recent"}


# ── bitable 侧 ──


def test_bitable_purge_dry_run_two_pages_no_delete(monkeypatch):
    pages = [
        page([rec(f"rec{i}", OLD_EPOCH_MS if i < 100 else RECENT_ISO) for i in range(200)]),
        page([rec("recA", OLD_ISO), rec("recB", OLD_ISO), rec("recC", None)]),
    ]
    calls, deletes = install_fake_lark(monkeypatch, pages)

    deleted, expired, scanned = bitable_purge.purge_expired_records(
        "app", "tbl", "2025-09-07", dry_run=True
    )
    assert (deleted, expired, scanned) == (0, 102, 203)
    assert not any("+record-delete" in c for c in calls)
    assert deletes == []
    assert sum(1 for c in calls if "+record-list" in c) == 2


def test_bitable_purge_apply_batches_200_with_yes(monkeypatch):
    pages = [
        page([rec(f"rec{i}", OLD_ISO) for i in range(200)]),
        page([rec(f"rec{200+i}", OLD_EPOCH_MS) for i in range(50)]),
    ]
    calls, deletes = install_fake_lark(monkeypatch, pages)

    deleted, expired, scanned = bitable_purge.purge_expired_records(
        "app", "tbl", "2025-09-07", dry_run=False
    )
    assert (deleted, expired, scanned) == (250, 250, 250)
    assert len(deletes) == 2
    assert len(deletes[0]["record_id_list"]) == 200
    assert len(deletes[1]["record_id_list"]) == 50
    delete_calls = [c for c in calls if "+record-delete" in c]
    assert all("--yes" in c for c in delete_calls)


def test_bitable_purge_batch_failure_stops(monkeypatch):
    pages = [
        page([rec(f"rec{i}", OLD_ISO) for i in range(200)]),
        page([rec(f"rec{200+i}", OLD_ISO) for i in range(50)]),
    ]
    calls, deletes = install_fake_lark(monkeypatch, pages, delete_fail=True)

    deleted, expired, _ = bitable_purge.purge_expired_records(
        "app", "tbl", "2025-09-07", dry_run=False
    )
    assert (deleted, expired) == (0, 250)
    assert sum(1 for c in calls if "+record-delete" in c) == 1
    assert deletes == []


def test_bitable_purge_first_page_failure_safe(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(args, stdin_text=None, timeout=120):
        calls.append(list(args))
        return FakeProc(1, stderr="boom")

    monkeypatch.setattr(bitable, "_run", fake_run)
    assert bitable_purge.purge_expired_records("app", "tbl", "2025-09-07", dry_run=False) == (0, 0, 0)
    assert not any("+record-delete" in c for c in calls)


def test_bitable_purge_row_shape_fields_data(monkeypatch):
    row_page = json.dumps(
        {"ok": True, "data": {
            "fields": ["标题", "推送时间"],
            "data": [["a", "2025-01-01"], ["b", "2026-09-01"]],
            "record_ids": ["recA", "recB"],
        }},
        ensure_ascii=False,
    )
    install_fake_lark(monkeypatch, [row_page])

    deleted, expired, scanned = bitable_purge.purge_expired_records(
        "app", "tbl", "2025-09-07", dry_run=True
    )
    assert (deleted, expired, scanned) == (0, 1, 2)


def test_cutoff_date_shanghai_boundary(monkeypatch):
    assert bitable_purge.cutoff_date_shanghai(365, now=NOW) == "2025-09-07"
    pages = [page([
        rec("recEq", "2025-09-07T08:00:00+08:00"),
        rec("recOld", "2025-09-06T23:00:00+08:00"),
    ])]
    install_fake_lark(monkeypatch, pages)
    _, expired, _ = bitable_purge.purge_expired_records("app", "tbl", "2025-09-07", dry_run=True)
    assert expired == 1


# ── run() 编排 ──


def test_purge_run_skips_bitable_when_disabled_or_placeholder(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("不应调用 bitable 清理")

    monkeypatch.setattr(bitable_purge, "purge_expired_records", boom)

    conn = make_conn()
    add_article(conn, "old-arch", "2025-01-01T00:00:00Z", "2025-01-02T00:00:00Z")
    stats = purge.run(make_cfg(enabled=False), conn, dry_run=False, now=NOW)
    assert stats.bitable_skipped_reason == "bitable未启用"
    assert stats.sqlite_deleted == 1

    conn2 = make_conn()
    add_article(conn2, "old-arch", "2025-01-01T00:00:00Z", "2025-01-02T00:00:00Z")
    stats2 = purge.run(
        make_cfg(enabled=True, app_token="<app>", table_id="<tbl>"), conn2, dry_run=False, now=NOW
    )
    assert stats2.bitable_skipped_reason == "占位 token 未替换"


def test_purge_run_apply_writes_meta_and_calls_bitable(monkeypatch):
    captured = {}

    def fake_purge(app_token, table_id, cutoff_date, dry_run=False):
        captured["args"] = (app_token, table_id, cutoff_date, dry_run)
        return 3, 5, 50

    monkeypatch.setattr(bitable_purge, "purge_expired_records", fake_purge)

    conn = make_conn()
    cfg = make_cfg(enabled=True, app_token="appReal", table_id="tblReal")
    stats = purge.run(cfg, conn, dry_run=False, now=NOW)
    assert captured["args"] == ("appReal", "tblReal", "2025-09-07", False)
    assert (stats.bitable_deleted, stats.bitable_expired, stats.bitable_scanned) == (3, 5, 50)
    assert store.get_meta(conn, purge.PURGE_LAST_RUN_KEY)

    conn2 = make_conn()
    purge.run(cfg, conn2, dry_run=True, now=NOW)
    assert store.get_meta(conn2, purge.PURGE_LAST_RUN_KEY) == ""


def test_retention_days_default_and_override():
    assert BitableConf().retention_days == 365
    conn = make_conn()
    add_article(conn, "mid", "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z")

    stats = purge.run(make_cfg(enabled=False), conn, dry_run=True, now=NOW)
    assert stats.retention_days == 365
    assert stats.sqlite_deleted == 0
    assert stats.sqlite_expired_unarchived == 0

    conn2 = make_conn()
    add_article(conn2, "mid", "2026-08-01T00:00:00Z", "2026-08-02T00:00:00Z")
    stats2 = purge.run(make_cfg(enabled=False), conn2, dry_run=False, retention_days=30, now=NOW)
    assert stats2.retention_days == 30
    assert stats2.sqlite_deleted == 1
    assert stats2.cutoff_iso == "2026-08-08T02:00:00Z"


# ── CLI ──


def test_purge_cli_dry_run_default_and_apply(monkeypatch, tmp_path, capsys):
    db = tmp_path / "t.sqlite3"

    def fake_load(config_path, db_path, app_env=None):
        cfg = make_cfg(enabled=False)
        cfg.db_path = db_path
        return cfg

    monkeypatch.setattr(purge, "load_config", fake_load)

    rc = purge.main(["--env", "test", "--db", str(db)])
    assert rc == 0
    out = capsys.readouterr().out
    stats = json.loads(out[out.index("{") :])
    assert stats["dry_run"] is True
    conn = store.connect(db)
    assert store.get_meta(conn, purge.PURGE_LAST_RUN_KEY) == ""
    conn.close()

    rc = purge.main(["--apply", "--env", "test", "--db", str(db)])
    assert rc == 0
    out = capsys.readouterr().out
    stats = json.loads(out[out.index("{") :])
    assert stats["dry_run"] is False
    conn = store.connect(db)
    assert store.get_meta(conn, purge.PURGE_LAST_RUN_KEY)
    conn.close()


def test_purge_cli_retention_days_override(monkeypatch, tmp_path, capsys):
    def fake_load(config_path, db_path, app_env=None):
        cfg = make_cfg(enabled=False)
        cfg.db_path = db_path
        return cfg

    monkeypatch.setattr(purge, "load_config", fake_load)
    rc = purge.main(["--retention-days", "30", "--env", "test", "--db", str(tmp_path / "t.db")])
    assert rc == 0
    out = capsys.readouterr().out
    stats = json.loads(out[out.index("{") :])
    assert stats["retention_days"] == 30
