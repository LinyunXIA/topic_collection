"""tc-bitable CLI dry-run 安全测试（#158）。

dry-run 必须零写：不建 Base（不调 ensure_initialized）、不清空（不调
+record-delete）、不写入（不调 sync_env）；lark-cli 子进程全部经
monkeypatch bitable._run 拦截，不触网、不碰真实库/Base。
"""

from __future__ import annotations

from pathlib import Path

from feedkicker import bitable
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
    monkeypatch.setattr("feedkicker.config.load_config", lambda **kw: cfg)

    def fake_run(args, stdin_text=None, timeout=120):
        calls.append(list(args))
        if "+record-list" in args:
            return FakeProc(0, stdout=PAGE)
        return FakeProc(0, stdout="{}")

    monkeypatch.setattr(bitable, "_run", fake_run)
    return cfg


def forbid(monkeypatch, *names):
    def boom(name):
        def _fail(*a, **k):
            raise AssertionError(f"dry-run 不得调用 {name}")

        return _fail

    for name in names:
        monkeypatch.setattr(bitable, name, boom(name))


def test_purge_all_records_dry_run_counts_without_delete(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(args, stdin_text=None, timeout=120):
        calls.append(list(args))
        return FakeProc(0, stdout=PAGE)

    monkeypatch.setattr(bitable, "_run", fake_run)
    assert bitable.purge_all_records("app", "tbl", dry_run=True) == 2
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
    calls: list[list[str]] = []
    install_cfg_and_lark(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(
        bitable,
        "ensure_initialized",
        lambda bt, env: {"app_token": "appReal", "table_id": "tblReal", "url": "https://x"},
    )
    synced: list[str] = []
    monkeypatch.setattr(bitable, "sync_env", lambda bt, env, conn: synced.append(env) or 2)

    assert bitable.main(["--reseed", "--env", "test"]) == 0
    assert any("+record-delete" in c for c in calls)
    assert synced == ["test"]
