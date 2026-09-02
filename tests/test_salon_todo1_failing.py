from __future__ import annotations

import sqlite3

import pytest


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


def test_store_ppt_synced_column_exists():
    from feedkicker import store

    conn = store.connect(":memory:")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(articles)")]
    assert "ppt_synced_at" in cols
    conn.close()


def test_store_select_unsynced_topics_and_mark():
    from feedkicker import store

    conn = store.connect(":memory:")
    store.download(conn, "F", [
        {"entry_key": "k1", "title": "t1", "url": "https://e.com/1", "description": "", "published_at": None},
        {"entry_key": "k2", "title": "t2", "url": "https://e.com/2", "description": "", "published_at": None},
    ], "2026-08-25T00:00:00Z")
    unsynced = store.select_unsynced_topics(conn)
    assert len(unsynced) == 2
    store.mark_ppt_synced(conn, unsynced, "2026-08-25T01:00:00Z")
    assert store.select_unsynced_topics(conn) == []
    cols = {r[1] for r in conn.execute("PRAGMA table_info(articles)")}
    assert "ppt_synced_at" in cols
    conn.close()


def test_store_ppt_last_status_via_meta():
    from feedkicker import store

    conn = store.connect(":memory:")
    if hasattr(store, "set_ppt_last_status") and hasattr(store, "get_ppt_last_status"):
        store.set_ppt_last_status(conn, "rec123", "已选题")
        assert store.get_ppt_last_status(conn, "rec123") == "已选题"
        assert store.get_ppt_last_status(conn, "rec999") == ""
    else:
        store.set_meta(conn, "ppt_last_status_rec123", "已选题")
        assert store.get_meta(conn, "ppt_last_status_rec123") == "已选题"
    conn.close()
