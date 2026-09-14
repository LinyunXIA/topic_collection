"""第七轮审计 P3-B（#307/#309/#315/#317）——失败优先验收。

全离线：lark-cli/httpx 一律 mock。文档类 #295/#296/#310–#314/#318 无回归测试。
"""

from __future__ import annotations

import inspect

from feedkicker import bitable, bitable_backfill, bitable_records, bitable_schema, purge
from feedkicker.config import Config, load_config
from feedkicker.extract_write import link_keys


class FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ── #307 --retention-days 上界 36500，超限 rc2 ──


def test_purge_retention_days_over_bound_rc2(capsys) -> None:
    assert purge.main(["--retention-days", str(10**9), "--env", "test"]) == 2


def test_purge_retention_days_in_range_ok(monkeypatch, tmp_path) -> None:
    cfg = Config(app_env="test")
    cfg.db_path = str(tmp_path / "p.sqlite3")
    monkeypatch.setattr("feedkicker.config.load_config", lambda *a, **kw: cfg)

    assert purge.main(["--retention-days", "36500", "--env", "test"]) == 0


# ── #309 backfill 使用显式 dry_run=False（死传参消除） ──


def test_backfill_uses_explicit_false_dry_run(monkeypatch, tmp_path) -> None:
    cfg = Config(app_env="test")
    cfg.bitable.enabled = True
    cfg.bitable.app_token = "appReal"
    cfg.bitable.table_id = "tblReal"
    cfg.db_path = str(tmp_path / "b.sqlite3")
    monkeypatch.setattr("feedkicker.config.load_config", lambda *a, **kw: cfg)
    monkeypatch.setattr(
        bitable_schema, "ensure_initialized",
        lambda *a, **kw: {"app_token": "appReal", "table_id": "tblReal", "url": ""},
    )
    seen: dict = {}
    monkeypatch.setattr(
        bitable_backfill, "backfill_empty_archive_dates",
        lambda *a, **kw: seen.update(kw) or 0,
    )
    monkeypatch.setattr(bitable_records, "sync_env", lambda *a, **k: 0)

    assert bitable.main(["--backfill", "--env", "test"]) == 0
    assert seen.get("dry_run") is False
    assert "dry_run=args.dry_run" not in inspect.getsource(bitable.main)


# ── #315 markdown 目标含一层括号不截断 ──


def test_link_keys_balanced_paren_url() -> None:
    wrapped = "[x](https://en.wikipedia.org/wiki/Foo_(bar))"
    bare = "https://en.wikipedia.org/wiki/Foo_(bar)"

    assert link_keys(wrapped) == link_keys(bare) == {"https://en.wikipedia.org/wiki/Foo_(bar)"}


def test_link_keys_adjacent_links_still_split() -> None:
    assert link_keys("[a](https://e.com/1)[b](https://e.com/2)") == {
        "https://e.com/1",
        "https://e.com/2",
    }


# ── #317 内嵌占位 webhook 也须清空 ──


def test_inline_placeholder_webhook_cleared(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("FEISHU_WEBHOOK", raising=False)
    monkeypatch.delenv("FEISHU_SECRET", raising=False)
    path = tmp_path / "cfg.yaml"
    path.write_text(
        'feishu_webhook: "https://open.feishu.cn/open-apis/bot/v2/hook/<prod-token>"\n'
        'feishu_secret: "<secret>"\n',
        encoding="utf-8",
    )

    cfg = load_config(config_path=path, app_env="test")

    assert cfg.feishu_webhook == ""
    assert cfg.feishu_secret == ""
