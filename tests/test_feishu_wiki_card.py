from __future__ import annotations

import json

import pytest

from feedkicker import feishu, store
from feedkicker.config import load_config


def _cfg(monkeypatch):
    monkeypatch.delenv("MiniMax_Key", raising=False)
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    cfg = load_config(app_env="test")
    cfg.salon.app_token = "TikpbwV0oaFAnYsoMCxchMRyncr"
    cfg.salon.table_id = "tblNPcbupKIBzLAx"
    cfg.salon.wiki_space_id = "spc_test"
    cfg.salon.wiki_parent_token = "parent_test"
    cfg.wiki.space_id = "spc_test"
    cfg.wiki.parent_token = "parent_test"
    cfg.wiki.app_token = "app_wiki"
    cfg.minimax.api_key = "sk-test"
    cfg.feishu_webhook = "https://hook.test"
    cfg.feishu_secret = "sec"
    cfg.http.timeout_seconds = 5
    cfg.http.user_agent = "test"
    return cfg


def test_build_card_wiki_single_button_and_lark_md():
    url = "https://web91vfvm7.feishu.cn/wiki/wik123"
    card = feishu.build_card([], 0, [], wiki_urls=[url])
    elements = card["card"]["elements"]
    actions = [e for e in elements if e["tag"] == "action"]
    assert len(actions) == 1
    assert actions[0]["actions"][0]["text"]["content"] == "📖 查看大纲"
    assert actions[0]["actions"][0]["url"] == url
    md_divs = [e for e in elements if e["tag"] == "div" and "[📖 查看大纲](" in e["text"]["content"]]
    assert len(md_divs) == 1
    assert url in md_divs[0]["text"]["content"]
    stripped = feishu.strip_actions(card)
    assert all(e["tag"] != "action" for e in stripped["card"]["elements"])
    assert any("[📖 查看大纲](" in e.get("text", {}).get("content", "") for e in stripped["card"]["elements"])


def test_build_card_wiki_multiple_numbered():
    urls = [f"https://web91vfvm7.feishu.cn/wiki/wik{i}" for i in range(1, 4)]
    card = feishu.build_card([], 0, [], wiki_urls=urls)
    actions = [e for e in card["card"]["elements"] if e["tag"] == "action"]
    assert len(actions) == 3
    labels = [a["actions"][0]["text"]["content"] for a in actions]
    assert labels == ["📖 查看大纲 1", "📖 查看大纲 2", "📖 查看大纲 3"]
    md_contents = " ".join(e["text"]["content"] for e in card["card"]["elements"] if e["tag"] == "div")
    for idx, url in enumerate(urls, 1):
        assert f"[📖 查看大纲 {idx}]({url})" in md_contents
    stripped = feishu.strip_actions(card)
    assert all(e["tag"] != "action" for e in stripped["card"]["elements"])


def test_build_card_wiki_plus_detail_url_both():
    card = feishu.build_card(
        [{"feed_id": "F", "entry_key": "k", "title": "t", "url": "https://e.com/1", "description": ""}],
        0,
        ["F"],
        detail_url="https://example.com/detail",
        detail_label="📰 详情见多维表格",
        wiki_urls=["https://web91vfvm7.feishu.cn/wiki/wikA"],
    )
    actions = [e for e in card["card"]["elements"] if e["tag"] == "action"]
    assert len(actions) == 2
    urls = [a["actions"][0]["url"] for a in actions]
    assert "https://example.com/detail" in urls
    assert "https://web91vfvm7.feishu.cn/wiki/wikA" in urls
    md_contents = " ".join(e["text"]["content"] for e in card["card"]["elements"] if e["tag"] == "div")
    assert "[📰 详情见多维表格](https://example.com/detail)" in md_contents
    assert "[📖 查看大纲](https://web91vfvm7.feishu.cn/wiki/wikA)" in md_contents


def test_build_card_wiki_custom_label():
    card = feishu.build_card([], 0, [], wiki_urls=["https://w.cn/1"], wiki_label="自定义")
    actions = [e for e in card["card"]["elements"] if e["tag"] == "action"]
    assert actions[0]["actions"][0]["text"]["content"] == "自定义"
    assert "[自定义](https://w.cn/1)" in " ".join(e["text"]["content"] for e in card["card"]["elements"] if e["tag"] == "div")


def test_build_card_wiki_20kb_downgrade():
    big_items = [
        {
            "feed_id": "F",
            "entry_key": f"k{i}",
            "title": f"标题{i} " + "长标题" * 20,
            "url": f"https://e.com/{i}",
            "description": "描述 " * 200,
        }
        for i in range(30)
    ]
    card = feishu.build_card(
        big_items,
        0,
        ["F"],
        wiki_urls=[f"https://web91vfvm7.feishu.cn/wiki/wik{i}" for i in range(5)],
    )
    raw = json.dumps(card, ensure_ascii=False)
    assert len(raw.encode("utf-8")) <= 20000
    assert any("📖 查看大纲" in e.get("text", {}).get("content", "") for e in card["card"]["elements"])
    stripped_raw = json.dumps(feishu.strip_actions(card), ensure_ascii=False)
    assert len(stripped_raw.encode("utf-8")) <= 20000


def test_build_card_wiki_empty_content_fallback():
    card = feishu.build_card([], 0, [], wiki_urls=["https://w.cn/1"])
    first = card["card"]["elements"][0]
    assert first["tag"] == "div"
    assert "已生成 1 份大纲" in first["text"]["content"]


def test_salon_flow_dry_run_payload_contains_wiki_button(monkeypatch, capsys):
    from feedkicker import salon_flow as sf

    cfg = _cfg(monkeypatch)
    conn = store.connect(":memory:")
    monkeypatch.setattr(sf, "fetch_selected_topics", lambda app, tbl, limit=200: [
        {"record_id": "recGWg8Kb9kUDI", "fields": {"讨论状态": ["已选题"], "话题名称": "话题A"}},
    ])
    monkeypatch.setattr(sf.minimax, "gen_outline", lambda topic, kind="tool", api_key=None, base_url=None, model=None: {"title": f"{kind}大纲", "slides": [{"heading": "h1", "bullets": ["a", "b", "c"]}]})
    monkeypatch.setattr(sf.wiki, "create_wiki_doc_from_md", lambda app_token, space_id, parent_token, title, md_content, dry_run=False, date_str=None: "https://web91vfvm7.feishu.cn/wiki/wik_dry")
    rc = sf.run(cfg, conn, dry_run=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert "https://web91vfvm7.feishu.cn/wiki/wik_dry" in out
    assert "📖 查看大纲" in out
    assert "msg_type" in out and "interactive" in out
    assert store.get_ppt_last_status(conn, "recGWg8Kb9kUDI") == ""
    conn.close()


def test_salon_flow_push_sends_wiki_card(monkeypatch):
    from feedkicker import salon_flow as sf

    cfg = _cfg(monkeypatch)
    conn = store.connect(":memory:")
    monkeypatch.setattr(sf, "fetch_selected_topics", lambda app, tbl, limit=200: [
        {"record_id": "rec1", "fields": {"讨论状态": ["已选题"], "话题名称": "T1"}},
    ])
    monkeypatch.setattr(sf.minimax, "gen_outline", lambda topic, kind="tool", api_key=None, base_url=None, model=None: {"title": "t", "slides": [{"heading": "h", "bullets": ["a"]}]})
    monkeypatch.setattr(sf.wiki, "create_wiki_doc_from_md", lambda *a, **kw: "https://web91vfvm7.feishu.cn/wiki/wik_send")

    sends = []

    def fake_send(payload, webhook, timeout, ua, secret=""):
        sends.append((payload, webhook, timeout, ua, secret))
        return True

    monkeypatch.setattr(sf.feishu, "send", fake_send)
    rc = sf.run(cfg, conn, dry_run=False)
    assert rc == 0
    assert len(sends) == 1
    payload, webhook, timeout, ua, secret = sends[0]
    assert webhook == "https://hook.test"
    assert timeout == 5
    md = json.dumps(payload, ensure_ascii=False)
    assert "https://web91vfvm7.feishu.cn/wiki/wik_send" in md
    assert "📖 查看大纲" in md
    md_elements = [e for e in payload["card"]["elements"] if e["tag"] == "action"]
    assert len(md_elements) == 1
    assert md_elements[0]["actions"][0]["url"] == "https://web91vfvm7.feishu.cn/wiki/wik_send"
    conn.close()


def test_salon_flow_push_strip_actions_retry(monkeypatch):
    from feedkicker import salon_flow as sf

    cfg = _cfg(monkeypatch)
    conn = store.connect(":memory:")
    monkeypatch.setattr(sf, "fetch_selected_topics", lambda app, tbl, limit=200: [
        {"record_id": "rec1", "fields": {"讨论状态": ["已选题"], "话题名称": "T1"}},
    ])
    monkeypatch.setattr(sf.minimax, "gen_outline", lambda topic, kind="tool", api_key=None, base_url=None, model=None: {"title": "t", "slides": [{"heading": "h", "bullets": ["a"]}]})
    monkeypatch.setattr(sf.wiki, "create_wiki_doc_from_md", lambda *a, **kw: "https://web91vfvm7.feishu.cn/wiki/wik_retry")

    calls = []

    def fake_send(payload, webhook, timeout, ua, secret=""):
        calls.append(payload)
        if len(calls) == 1:
            return False
        assert all(e["tag"] != "action" for e in payload["card"]["elements"])
        return True

    monkeypatch.setattr(sf.feishu, "send", fake_send)
    rc = sf.run(cfg, conn, dry_run=False)
    assert rc == 0
    assert len(calls) == 2
    first_has_action = any(e["tag"] == "action" for e in calls[0]["card"]["elements"])
    second_has_action = any(e["tag"] == "action" for e in calls[1]["card"]["elements"])
    assert first_has_action is True
    assert second_has_action is False
    assert any("[📖 查看大纲](" in e.get("text", {}).get("content", "") for e in calls[1]["card"]["elements"])
    conn.close()


def test_salon_flow_no_wiki_no_send(monkeypatch):
    from feedkicker import salon_flow as sf

    cfg = _cfg(monkeypatch)
    conn = store.connect(":memory:")
    monkeypatch.setattr(sf, "fetch_selected_topics", lambda app, tbl, limit=200: [])
    monkeypatch.setattr(sf.feishu, "send", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not send")))
    rc = sf.run(cfg, conn, dry_run=False)
    assert rc == 0
    conn.close()


def test_salon_flow_dry_run_no_send_network(monkeypatch):
    from feedkicker import salon_flow as sf

    cfg = _cfg(monkeypatch)
    cfg.feishu_webhook = "https://hook.test"
    conn = store.connect(":memory:")
    monkeypatch.setattr(sf, "fetch_selected_topics", lambda app, tbl, limit=200: [
        {"record_id": "rec1", "fields": {"讨论状态": ["已选题"], "话题名称": "T1"}},
    ])
    monkeypatch.setattr(sf.minimax, "gen_outline", lambda topic, kind="tool", api_key=None, base_url=None, model=None: {"title": "t", "slides": [{"heading": "h", "bullets": ["a"]}]})
    monkeypatch.setattr(sf.wiki, "create_wiki_doc_from_md", lambda *a, **kw: "https://web91vfvm7.feishu.cn/wiki/wik_nosend")
    monkeypatch.setattr(sf.feishu, "send", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("dry-run must not call send")))
    rc = sf.run(cfg, conn, dry_run=True)
    assert rc == 0
    conn.close()


def test_salon_flow_uses_same_webhook_as_push(monkeypatch):
    from feedkicker import salon_flow as sf

    cfg = _cfg(monkeypatch)
    cfg.feishu_webhook = "https://hook.test/salon_same"
    conn = store.connect(":memory:")
    monkeypatch.setattr(sf, "fetch_selected_topics", lambda app, tbl, limit=200: [
        {"record_id": "rec1", "fields": {"讨论状态": ["已选题"], "话题名称": "T1"}},
    ])
    monkeypatch.setattr(sf.minimax, "gen_outline", lambda topic, kind="tool", api_key=None, base_url=None, model=None: {"title": "t", "slides": [{"heading": "h", "bullets": ["a"]}]})
    monkeypatch.setattr(sf.wiki, "create_wiki_doc_from_md", lambda *a, **kw: "https://web91vfvm7.feishu.cn/wiki/wik_same")
    captured = {}

    def fake_send(payload, webhook, timeout, ua, secret=""):
        captured["webhook"] = webhook
        return True

    monkeypatch.setattr(sf.feishu, "send", fake_send)
    sf.run(cfg, conn, dry_run=False)
    assert captured["webhook"] == "https://hook.test/salon_same"
    conn.close()
