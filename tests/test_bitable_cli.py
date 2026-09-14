"""tc-bitable CLI dry-run 安全测试（#158）。

dry-run 必须零写：不建 Base（不调 ensure_initialized）、不清空（不调
+record-delete）、不写入（不调 sync_env）；lark-cli 子进程全部经
monkeypatch bitable_lark._run 拦截，不触网、不碰真实库/Base。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from feedkicker import bitable, bitable_lark, bitable_records, bitable_schema, bitable_views, store
from feedkicker.config import (
    BitableConf,
    Config,
    HttpConf,
    MinimaxConf,
    SalonConf,
    SiteConf,
    WikiConf,
)

PAGE = "| _record_id | 标题 |\n| --- | --- |\n| recAAA | a |\n| recBBB | b |"


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def make_cfg(tmp_path, **bt_kw) -> Config:
    cfg = Config(app_env="test")
    cfg.http = HttpConf()
    cfg.site = SiteConf()
    cfg.bitable = BitableConf(**bt_kw)
    cfg.salon = SalonConf()
    cfg.minimax = MinimaxConf()
    cfg.wiki = WikiConf()
    cfg.db_path = str(tmp_path / "t.sqlite3")
    return cfg


def install_cfg_and_lark(monkeypatch, tmp_path, calls, **bt_kw) -> Config:
    cfg = make_cfg(tmp_path, enabled=True, app_token="appReal", table_id="tblReal", **bt_kw)
    monkeypatch.setattr("feedkicker.config.load_config", lambda *a, **kw: cfg)

    def fake_run(args, stdin_text=None, timeout=120):
        calls.append(list(args))
        if "+record-list" in args:
            return FakeProc(0, stdout=json.dumps({"data": {"records": []}}, ensure_ascii=False))
        return FakeProc(0, stdout="{}")

    monkeypatch.setattr(bitable_lark, "_run", fake_run)
    return cfg


def forbid(monkeypatch, *names):
    def boom(name):
        def _fail(*a, **k):
            raise AssertionError(f"dry-run 不得调用 {name}")

        return _fail

    owners = {
        "ensure_initialized": bitable_schema,
        "sync_env": bitable_records,
        "ensure_archive_date_field": bitable_views,
        "setup_view": bitable_views,
        "create_date_view": bitable_views,
        "set_tenant_readonly": bitable_views,
    }
    for name in names:
        monkeypatch.setattr(owners[name], name, boom(name))


def test_purge_all_records_dry_run_counts_without_delete(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(args, stdin_text=None, timeout=120):
        calls.append(list(args))
        return FakeProc(0, stdout=PAGE)

    monkeypatch.setattr(bitable_lark, "_run", fake_run)
    assert bitable.purge_all_records("app", "tbl", dry_run=True) == (2, True)
    assert not any("+record-delete" in c for c in calls)


def test_reseed_dry_run_zero_writes(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    cfg = install_cfg_and_lark(monkeypatch, tmp_path, calls)
    forbid(monkeypatch, "ensure_initialized", "sync_env")

    assert bitable.main(["--reseed", "--dry-run", "--env", "test"]) == 0
    assert not any("+record-delete" in c or "+record-batch-create" in c for c in calls)
    assert any("+record-list" in c for c in calls)
    assert not Path(cfg.db_path).exists()


def test_init_dry_run_zero_writes(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    install_cfg_and_lark(monkeypatch, tmp_path, calls)
    forbid(
        monkeypatch,
        "ensure_initialized",
        "ensure_archive_date_field",
        "setup_view",
        "create_date_view",
        "set_tenant_readonly",
        "sync_env",
    )

    assert bitable.main(["--init", "--dry-run", "--env", "test"]) == 0
    assert calls == []


def test_dry_run_alone_creates_nothing(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    cfg = install_cfg_and_lark(monkeypatch, tmp_path, calls)
    forbid(monkeypatch, "ensure_initialized", "sync_env")

    assert bitable.main(["--dry-run", "--env", "test"]) == 0
    assert calls == []
    assert not Path(cfg.db_path).exists()


def test_reseed_without_dry_run_keeps_purge_and_sync(monkeypatch, tmp_path):
    """#218：test 环境 reseed 走「环境」过滤的 JSON 路径，批删后照常 sync。"""
    calls: list[list[str]] = []

    def fake_run(args, stdin_text=None, timeout=120):
        calls.append(list(args))
        if "+record-list" in args:
            if "环境" in args:
                records = [{"record_id": "recOld", "fields": {"环境": "test", "链接": "https://old.example/1"}}]
                return FakeProc(0, stdout=json.dumps({"data": {"records": records}}, ensure_ascii=False))
            return FakeProc(0, stdout=json.dumps({"data": {"records": []}}))
        return FakeProc(0, stdout="{}")

    install_cfg_and_lark(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(bitable_lark, "_run", fake_run)
    monkeypatch.setattr(
        bitable_schema,
        "ensure_initialized",
        lambda bt, env: {"app_token": "appReal", "table_id": "tblReal", "url": "https://x"},
    )
    synced: list[str] = []
    monkeypatch.setattr(bitable_records, "sync_env", lambda bt, env, conn: synced.append(env) or 2)

    assert bitable.main(["--reseed", "--env", "test"]) == 0
    assert any("+record-delete" in c for c in calls)
    assert synced == ["test"]


def test_reseed_resets_synced_marks_and_reloads(monkeypatch, tmp_path):
    """#197：已同步行在 --reseed 后必须重灌；#196：salon 占位行不参与。"""
    calls: list[list[str]] = []
    payloads: list[dict] = []
    cfg = make_cfg(tmp_path, enabled=True, app_token="appReal", table_id="tblReal")
    monkeypatch.setattr("feedkicker.config.load_config", lambda *a, **kw: cfg)
    monkeypatch.setattr(
        bitable_schema,
        "ensure_initialized",
        lambda bt, env: {"app_token": "appReal", "table_id": "tblReal", "url": "https://x"},
    )

    def fake_run(args, stdin_text=None, timeout=120):
        calls.append(list(args))
        if "+record-list" in args and "环境" in args:
            records = [{"record_id": "recEnvOld", "fields": {"环境": "test", "链接": "https://old.example/1"}}]
            return FakeProc(0, stdout=json.dumps({"data": {"records": records}}, ensure_ascii=False))
        if "+record-list" in args:
            return FakeProc(0, stdout=json.dumps({"data": {"records": []}}))
        if "+record-batch-create" in args:
            raw = args[args.index("--json") + 1]
            payloads.append(json.loads(Path(raw[1:]).read_text(encoding="utf-8")))
            return FakeProc(0, stdout="{}")
        return FakeProc(0, stdout="{}")

    monkeypatch.setattr(bitable_lark, "_run", fake_run)

    conn = store.connect(cfg.db_path)
    store.download(
        conn,
        "F",
        [{"entry_key": "k1", "title": "真实资讯", "url": "https://e.com/1", "description": "", "published_at": None}],
        "2026-09-14T00:00:00Z",
    )
    conn.execute("UPDATE articles SET bitable_synced_at = ?", ("2026-09-14T01:00:00Z",))
    conn.commit()
    store.mark_topic_archived(
        conn, "tblSalon", "recStub000", "沙龙选题", "https://e.com/s", "摘录", "2026-09-14T02:00:00Z"
    )
    conn.close()

    assert bitable.main(["--reseed", "--env", "test"]) == 0
    assert any("+record-delete" in c for c in calls)
    flat = [rec for p in payloads for rec in p["create_records"]]
    assert [rec["链接"] for rec in flat] == ["https://e.com/1"]

    conn2 = store.connect(cfg.db_path)
    row = conn2.execute("SELECT bitable_synced_at FROM articles WHERE entry_key = 'k1'").fetchone()
    assert row[0] is not None
    conn2.close()


def test_main_accepts_config_and_db(monkeypatch, tmp_path):
    loaded: list[tuple] = []
    cfg = make_cfg(tmp_path, enabled=True, app_token="appReal", table_id="tblReal")

    def fake_load(*a, **kw):
        loaded.append((a, kw))
        return cfg

    monkeypatch.setattr("feedkicker.config.load_config", fake_load)
    monkeypatch.setattr(bitable_lark, "_run", lambda *a, **kw: FakeProc(0, stdout=PAGE))
    cfg_file = tmp_path / "config-test.yaml"
    db_file = tmp_path / "custom.sqlite3"

    rc = bitable.main(
        ["--dry-run", "--config", str(cfg_file), "--db", str(db_file), "--env", "test"]
    )
    assert rc == 0
    assert loaded == [((str(cfg_file), str(db_file)), {"app_env": "test"})]


@pytest.mark.parametrize(("app_token", "table_id"), [("", ""), ("<app>", "<tbl>")])
def test_reseed_rejected_without_configured_base(
    monkeypatch, tmp_path, app_token, table_id
):
    calls: list[list[str]] = []
    cfg = make_cfg(tmp_path, enabled=True, app_token=app_token, table_id=table_id)
    monkeypatch.setattr("feedkicker.config.load_config", lambda *a, **kw: cfg)
    monkeypatch.setattr(
        bitable_lark, "_run", lambda *a, **kw: calls.append(list(a)) or FakeProc(0, "{}")
    )
    forbid(monkeypatch, "ensure_initialized", "sync_env")

    assert bitable.main(["--reseed", "--env", "test"]) == 2
    assert calls == []
    assert not Path(cfg.db_path).exists()


def test_reseed_dry_run_placeholder_still_previews(monkeypatch, tmp_path):
    cfg = make_cfg(tmp_path, enabled=True, app_token="<app>", table_id="<tbl>")
    monkeypatch.setattr("feedkicker.config.load_config", lambda *a, **kw: cfg)
    monkeypatch.setattr(bitable_lark, "_run", lambda *a, **kw: FakeProc(0, "{}"))
    forbid(monkeypatch, "ensure_initialized", "sync_env")

    assert bitable.main(["--reseed", "--dry-run", "--env", "test"]) == 0
    assert not Path(cfg.db_path).exists()


def test_init_with_placeholder_warns_before_create(monkeypatch, tmp_path, caplog):
    cfg = make_cfg(tmp_path, enabled=True, app_token="", table_id="")
    monkeypatch.setattr("feedkicker.config.load_config", lambda *a, **kw: cfg)
    ensure_calls: list[str] = []
    monkeypatch.setattr(
        bitable_schema,
        "ensure_initialized",
        lambda bt, env: ensure_calls.append(env)
        or {"app_token": "appNew", "table_id": "tblNew", "url": "u"},
    )
    monkeypatch.setattr(bitable_views, "ensure_archive_date_field", lambda a, t: True)
    monkeypatch.setattr(bitable_views, "setup_view", lambda a, t: True)
    monkeypatch.setattr(bitable_views, "create_date_view", lambda a, t: True)
    monkeypatch.setattr(bitable_views, "set_tenant_readonly", lambda a: True)
    monkeypatch.setattr(bitable_records, "sync_env", lambda bt, env, conn: 0)

    with caplog.at_level(logging.WARNING, logger="feedkicker.bitable"):
        assert bitable.main(["--init", "--env", "test"]) == 0
    assert ensure_calls == ["test"]
    assert any("创建" in r.getMessage() for r in caplog.records)


def test_init_with_placeholder_tokens_rejects_rc2_without_lark(monkeypatch, tmp_path, caplog):
    """#262：`.example` 默认态占位 token 在 --init 下必须 rc2、零 lark 调用，不得静默走到建表/同步。"""
    calls: list[list[str]] = []
    cfg = make_cfg(tmp_path, enabled=True, app_token="<app_token>", table_id="<table_id>")
    monkeypatch.setattr("feedkicker.config.load_config", lambda *a, **kw: cfg)
    monkeypatch.setattr(bitable_lark, "lark_bin", lambda: "/fake/lark-cli")
    monkeypatch.setattr(
        bitable_lark, "_run", lambda *a, **kw: calls.append(list(a)) or FakeProc(0, "{}")
    )
    forbid(monkeypatch, "ensure_initialized", "sync_env")

    with caplog.at_level(logging.INFO, logger="feedkicker.bitable"):
        rc = bitable.main(["--init", "--env", "test"])

    assert rc == 2
    assert calls == []
    assert not Path(cfg.db_path).exists()
    assert not any("同步完成" in r.getMessage() for r in caplog.records)
