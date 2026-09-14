from __future__ import annotations

import pytest

from feedkicker import store
from feedkicker.config import PROJECT_ROOT, BitableConf, config_path_for, load_config


def test_baseline_load_config_test_defaults():
    cfg = load_config(app_env="test")
    assert cfg.app_env == "test"
    assert cfg.bootstrap_days == 1
    assert cfg.http.timeout_seconds == 20
    assert isinstance(cfg.bitable, BitableConf)
    assert cfg.bitable.enabled is True
    assert cfg.site.top_n == 5
    assert len(cfg.feeds) == 1
    assert cfg.feeds[0].name == "量子位"


def test_baseline_store_bitable_synced_column():
    conn = store.connect(":memory:")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(articles)")}
    assert "bitable_synced_at" in cols
    assert "title" in cols
    conn.close()


def test_baseline_store_select_unsynced_exist():
    conn = store.connect(":memory:")
    store.download(conn, "F", [{"entry_key": "k1", "title": "t", "url": "https://e.com/1", "description": "", "published_at": None}], "2026-08-25T00:00:00Z")
    assert len(store.select_unsynced(conn)) == 1
    assert hasattr(store, "mark_synced")
    assert hasattr(store, "get_meta")
    assert hasattr(store, "set_meta")
    conn.close()


def test_baseline_config_env_override_feishu(monkeypatch):
    monkeypatch.setenv("FEISHU_WEBHOOK", "https://hook.override/test")
    cfg = load_config(app_env="test")
    assert cfg.feishu_webhook == "https://hook.override/test"
    monkeypatch.delenv("FEISHU_WEBHOOK", raising=False)


def test_config_path_anchored_to_project_root_not_cwd(monkeypatch, tmp_path):
    """#163：默认 config 解析锚定仓库根，且仓外 cwd 仍能加载（conftest 注入）。"""
    monkeypatch.chdir(tmp_path)
    assert config_path_for("test") == PROJECT_ROOT / "config-test.yaml"
    assert load_config(app_env="test").app_env == "test"


def test_load_config_unknown_env_raises():
    with pytest.raises(ValueError, match="未知环境"):
        load_config(app_env="staging")


def test_load_config_feed_missing_url_raises(tmp_path):
    path = tmp_path / "feeds-broken.yaml"
    path.write_text("feeds:\n  - name: 只有名字\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"feeds\[0\] 缺少 url"):
        load_config(config_path=path, app_env="test")
