"""salon Wiki 卡片连败 SOS 行为测试（OBS1，salon_notify.send_wiki_card）。"""

from __future__ import annotations

from feedkicker import salon_notify, store


def _cfg(monkeypatch, webhook: str = "https://hook.test"):
    monkeypatch.delenv("MiniMax_Key", raising=False)
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    from feedkicker.config import load_config

    cfg = load_config(app_env="test")
    cfg.feishu_webhook = webhook
    cfg.feishu_secret = ""
    cfg.http.timeout_seconds = 20
    cfg.http.user_agent = "test"
    return cfg


def _patch_card(monkeypatch, send_ret, sos_texts):
    sends = []

    def fake_send(payload, *a, **kw):
        sends.append(payload)
        return send_ret

    monkeypatch.setattr(salon_notify.feishu, "send", fake_send)
    monkeypatch.setattr(
        salon_notify.feishu,
        "send_text",
        lambda text, *a, **kw: (sos_texts.append(text), True)[1],
    )
    return sends


def test_send_wiki_card_three_failures_triggers_sos_then_reset(monkeypatch):
    cfg = _cfg(monkeypatch)
    conn = store.connect(":memory:")
    sos_texts: list[str] = []
    sends = _patch_card(monkeypatch, False, sos_texts)

    rcs = [
        salon_notify.send_wiki_card(cfg, conn, [f"https://web91vfvm7.feishu.cn/wiki/wik{i}"])
        for i in range(3)
    ]
    assert rcs == [False, False, False]
    # 每班：初发 1 次 + strip_actions 降级重试 1 次
    assert len(sends) == 6
    assert len(sos_texts) == 1
    assert "连续 3 次" in sos_texts[0]
    assert store.get_meta(conn, salon_notify.SALON_FAIL_STREAK_KEY, "0") == "0"
    conn.close()


def test_send_wiki_card_recovery_clears_streak_without_sos(monkeypatch):
    cfg = _cfg(monkeypatch)
    conn = store.connect(":memory:")
    store.set_meta(conn, salon_notify.SALON_FAIL_STREAK_KEY, "2")
    sos_texts: list[str] = []
    _patch_card(monkeypatch, True, sos_texts)

    ok = salon_notify.send_wiki_card(cfg, conn, ["https://web91vfvm7.feishu.cn/wiki/wik1"])
    assert ok is True
    assert sos_texts == []
    assert store.get_meta(conn, salon_notify.SALON_FAIL_STREAK_KEY, "0") == "0"
    conn.close()


def test_send_wiki_card_first_two_failures_no_sos(monkeypatch):
    cfg = _cfg(monkeypatch)
    conn = store.connect(":memory:")
    sos_texts: list[str] = []
    _patch_card(monkeypatch, False, sos_texts)

    rcs = [
        salon_notify.send_wiki_card(cfg, conn, [f"https://web91vfvm7.feishu.cn/wiki/wik{i}"])
        for i in range(2)
    ]
    assert rcs == [False, False]
    assert sos_texts == []
    assert store.get_meta(conn, salon_notify.SALON_FAIL_STREAK_KEY, "0") == "2"
    conn.close()


def test_send_wiki_card_empty_urls_is_ok(monkeypatch):
    cfg = _cfg(monkeypatch)
    conn = store.connect(":memory:")
    sos_texts: list[str] = []
    sends = _patch_card(monkeypatch, False, sos_texts)

    assert salon_notify.send_wiki_card(cfg, conn, []) is True
    assert sends == []
    assert store.get_meta(conn, salon_notify.SALON_FAIL_STREAK_KEY, "MISSING") == "MISSING"
    conn.close()


def test_send_wiki_card_dry_run_no_send_no_sos_no_meta(monkeypatch, capsys):
    cfg = _cfg(monkeypatch)
    conn = store.connect(":memory:")

    def _guard(*a, **kw):
        raise AssertionError("dry-run 不得触网")

    monkeypatch.setattr(salon_notify.feishu, "send", _guard)
    monkeypatch.setattr(salon_notify.feishu, "send_text", _guard)

    ok = salon_notify.send_wiki_card(
        cfg, conn, ["https://web91vfvm7.feishu.cn/wiki/wik1"], dry_run=True
    )
    assert ok is True
    assert '"msg_type": "interactive"' in capsys.readouterr().out
    assert store.get_meta(conn, salon_notify.SALON_FAIL_STREAK_KEY, "MISSING") == "MISSING"
    conn.close()


def test_send_wiki_card_sos_requires_webhook(monkeypatch):
    cfg = _cfg(monkeypatch, webhook="")
    conn = store.connect(":memory:")
    sos_texts: list[str] = []
    sends = _patch_card(monkeypatch, False, sos_texts)

    for i in range(4):
        salon_notify.send_wiki_card(cfg, conn, [f"https://web91vfvm7.feishu.cn/wiki/wik{i}"])
    assert sos_texts == []
    assert len(sends) == 8
    assert store.get_meta(conn, salon_notify.SALON_FAIL_STREAK_KEY, "0") == "4"
    conn.close()
