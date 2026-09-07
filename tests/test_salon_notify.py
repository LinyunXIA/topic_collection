"""salon Wiki 卡片连败 SOS 行为测试（OBS1）。

Commit 1 阶段逻辑内联在 salon_flow.run()；Commit 2 抽到 salon_notify.send_wiki_card
后仅需把 sf.run 调用换成 send_wiki_card。
"""

from __future__ import annotations

from feedkicker import store


def _cfg(monkeypatch):
    from feedkicker.config import load_config

    monkeypatch.delenv("MiniMax_Key", raising=False)
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    cfg = load_config(app_env="test")
    cfg.salon.app_token = "app_test"
    cfg.salon.table_id = "tbl_test"
    cfg.salon.wiki_space_id = "spc_test"
    cfg.salon.wiki_parent_token = "parent_test"
    cfg.wiki.space_id = "spc_test"
    cfg.wiki.parent_token = "parent_test"
    cfg.wiki.app_token = "app_wiki"
    cfg.minimax.api_key = "sk-test"
    cfg.feishu_webhook = "https://hook.test"
    cfg.feishu_secret = ""
    cfg.http.timeout_seconds = 20
    cfg.http.user_agent = "test"
    return cfg


def _patch_pipeline(monkeypatch, sf):
    """每班返回一个全新已选题话题（已处理话题会被跳过），MiniMax/Wiki 全打桩。"""
    counter = {"n": 0}

    def fake_fetch(*a, **kw):
        counter["n"] += 1
        n = counter["n"]
        return [{"record_id": f"rec{n}", "fields": {"讨论状态": ["已选题"], "话题名称": f"话题{n}"}}]

    monkeypatch.setattr(sf, "fetch_selected_topics", fake_fetch)
    monkeypatch.setattr(
        sf.minimax,
        "gen_outline",
        lambda *a, **kw: {"title": "t", "slides": [{"heading": "h", "bullets": ["a"]}]},
    )
    monkeypatch.setattr(
        sf.wiki,
        "create_wiki_doc_from_md",
        lambda *a, **kw: f"https://web91vfvm7.feishu.cn/wiki/wik{counter['n']}",
    )
    return counter


def test_salon_card_three_failures_triggers_sos_then_reset(monkeypatch):
    from feedkicker import salon_flow as sf

    cfg = _cfg(monkeypatch)
    conn = store.connect(":memory:")
    _patch_pipeline(monkeypatch, sf)
    sends = []
    sos_texts = []
    monkeypatch.setattr(sf.feishu, "send", lambda *a, **kw: (sends.append(1), False)[1])
    monkeypatch.setattr(sf.feishu, "send_text", lambda text, *a, **kw: (sos_texts.append(text), True)[1])

    rcs = [sf.run(cfg, conn, dry_run=False) for _ in range(3)]
    assert rcs == [1, 1, 1]
    # 每班：初发 1 次 + strip_actions 降级重试 1 次
    assert len(sends) == 6
    assert len(sos_texts) == 1
    assert "连续 3 次" in sos_texts[0]
    assert store.get_meta(conn, sf.SALON_FAIL_STREAK_KEY, "0") == "0"
    conn.close()


def test_salon_card_recovery_clears_streak_without_sos(monkeypatch):
    from feedkicker import salon_flow as sf

    cfg = _cfg(monkeypatch)
    conn = store.connect(":memory:")
    store.set_meta(conn, sf.SALON_FAIL_STREAK_KEY, "2")
    _patch_pipeline(monkeypatch, sf)
    sos_texts = []
    monkeypatch.setattr(sf.feishu, "send", lambda *a, **kw: True)
    monkeypatch.setattr(sf.feishu, "send_text", lambda text, *a, **kw: (sos_texts.append(text), True)[1])

    rc = sf.run(cfg, conn, dry_run=False)
    assert rc == 0
    assert sos_texts == []
    assert store.get_meta(conn, sf.SALON_FAIL_STREAK_KEY, "0") == "0"
    conn.close()


def test_salon_card_first_two_failures_no_sos(monkeypatch):
    from feedkicker import salon_flow as sf

    cfg = _cfg(monkeypatch)
    conn = store.connect(":memory:")
    _patch_pipeline(monkeypatch, sf)
    sos_texts = []
    monkeypatch.setattr(sf.feishu, "send", lambda *a, **kw: False)
    monkeypatch.setattr(sf.feishu, "send_text", lambda text, *a, **kw: (sos_texts.append(text), True)[1])

    rcs = [sf.run(cfg, conn, dry_run=False) for _ in range(2)]
    assert rcs == [1, 1]
    assert sos_texts == []
    assert store.get_meta(conn, sf.SALON_FAIL_STREAK_KEY, "0") == "2"
    conn.close()


def test_salon_card_dry_run_no_send_no_sos_no_meta(monkeypatch):
    from feedkicker import salon_flow as sf

    cfg = _cfg(monkeypatch)
    conn = store.connect(":memory:")
    _patch_pipeline(monkeypatch, sf)

    def _guard(*a, **kw):
        raise AssertionError("dry-run 不得触网")

    monkeypatch.setattr(sf.minimax, "gen_outline", _guard)
    monkeypatch.setattr(sf.feishu, "send", _guard)
    monkeypatch.setattr(sf.feishu, "send_text", _guard)

    rc = sf.run(cfg, conn, dry_run=True)
    assert rc == 0
    assert store.get_meta(conn, sf.SALON_FAIL_STREAK_KEY, "MISSING") == "MISSING"
    conn.close()
