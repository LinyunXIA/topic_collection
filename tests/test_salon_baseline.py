"""salon 基线：配置默认值/环境覆盖与 store_salon 元数据（#165 由 todo1_failing 更名去重）。"""

from __future__ import annotations


def test_salon_config_defaults(monkeypatch):
    from feedkicker.config import load_config

    monkeypatch.delenv("MiniMax_Key", raising=False)
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.delenv("TC_SALON_TOKEN", raising=False)
    cfg = load_config(app_env="test")
    assert hasattr(cfg, "salon")
    assert hasattr(cfg, "minimax")
    assert hasattr(cfg, "wiki")
    assert cfg.salon.trigger_hour == 10
    assert cfg.salon.trigger_weekday == 4
    assert cfg.salon.trigger_minute == 0
    assert cfg.salon.enabled is False
    assert cfg.minimax.model == "MiniMax-M3"
    assert cfg.minimax.base_url == "https://api.minimaxi.com"
    assert cfg.minimax.api_key == ""


def test_salon_config_env_minimax_key_override(monkeypatch):
    from feedkicker.config import load_config

    monkeypatch.setenv("MiniMax_Key", "sk-test-123")
    cfg = load_config(app_env="test")
    assert cfg.minimax.api_key == "sk-test-123"
    monkeypatch.delenv("MiniMax_Key", raising=False)


def test_salon_config_env_tcsalontoken_override(monkeypatch):
    from feedkicker.config import load_config

    monkeypatch.setenv("TC_SALON_TOKEN", "salon-token-xyz")
    cfg = load_config(app_env="test")
    assert cfg.salon.app_token == "salon-token-xyz"
    monkeypatch.delenv("TC_SALON_TOKEN", raising=False)


def test_store_ppt_last_status_via_meta():
    from feedkicker import store

    conn = store.connect(":memory:")
    store.set_ppt_last_status(conn, "rec123", "已选题")
    assert store.get_ppt_last_status(conn, "rec123") == "已选题"
    assert store.get_ppt_last_status(conn, "rec999") == ""
    conn.close()
