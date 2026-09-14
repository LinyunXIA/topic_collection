"""第八轮审计 P2（#323–#329）——失败优先验收。全离线：lark-cli/httpx 一律 mock。"""

from __future__ import annotations

import json
import logging

import pytest

from feedkicker import fetch, store
from feedkicker import topic as topic_mod
from feedkicker.config import load_config as load_cfg
from feedkicker.extract_parse import md_link_tokens, parse_topics, strip_reasoning
from feedkicker.extract_write import link_keys, plan_writes
from feedkicker.feishu_card import build_card
from feedkicker.fetch import canonicalize


class FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _item(feed: str, key: str, title: str, url: str) -> dict:
    return {
        "feed_id": feed,
        "entry_key": key,
        "title": title,
        "url": url,
        "description": "",
        "published_at": None,
        "first_seen": None,
    }


# ── #323 自闭合/属性/空白开标签不得截断其后 JSON（#321 回归） ──


@pytest.mark.parametrize(
    "prefix",
    ["<think/>", "<think />", "<think >", "<think foo=1>", "<think\n>"],
)
def test_parse_topics_open_forms_do_not_swallow_json(prefix: str) -> None:
    assert parse_topics(prefix + '{"topics": []}') == ([], 0)


def test_strip_reasoning_removes_self_closing_tag() -> None:
    assert strip_reasoning('<think/>{"topics":[]}') == '{"topics":[]}'
    assert strip_reasoning('<think />{"topics":[]}') == '{"topics":[]}'


def test_strip_reasoning_paired_block_with_attrs() -> None:
    assert strip_reasoning('<think foo=1>a{1}</think>{"topics":[]}') == '{"topics":[]}'


def test_strip_reasoning_bare_unclosed_still_truncates() -> None:
    assert strip_reasoning("keep<thinking>reason {") == "keep"


# ── #324 跳过判据在 DB 写失败/查询异常下不得失效 ──


def test_is_ppt_synced_query_error_raises_not_false() -> None:
    conn = store.connect(":memory:")
    conn.close()

    with pytest.raises(RuntimeError):
        store.is_ppt_synced(conn, "recX")


def test_mark_topic_archived_first_segment_failure_raises_but_records_status() -> None:
    conn = store.connect(":memory:")
    conn.execute("CREATE TRIGGER boom BEFORE INSERT ON articles BEGIN SELECT RAISE(ABORT, 'boom'); END;")
    conn.commit()

    with pytest.raises(Exception):
        store.mark_topic_archived(
            conn, "tbl", "recX", "题", "https://wiki/x", "md", "2026-09-15T00:00:00Z"
        )

    assert store.get_ppt_last_status(conn, "recX") == "已选题"


def test_salon_flow_skips_when_only_last_status_set(monkeypatch) -> None:
    """#324：ppt_synced_at 缺失但 last_status=已选题（部分写残留）时跳过，不重建。"""
    from feedkicker import salon_flow as sf

    cfg = _sf_cfg(monkeypatch)
    conn = store.connect(":memory:")
    store.set_ppt_last_status(conn, "recL", "已选题")
    monkeypatch.setattr(
        sf,
        "fetch_selected_topics",
        lambda *a, **kw: [{"record_id": "recL", "fields": {"讨论状态": ["已选题"], "话题名称": "T"}}],
    )
    wiki_calls: list[int] = []
    monkeypatch.setattr(sf.wiki, "create_wiki_doc_from_md", lambda *a, **kw: wiki_calls.append(1) or "u")
    monkeypatch.setattr(sf.minimax, "gen_outline", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must skip")))
    monkeypatch.setattr(sf.salon_notify, "send_wiki_card", lambda *a, **kw: True)
    monkeypatch.setattr(sf.wiki_home, "update_homepage", lambda *a, **kw: True)

    sf.run(cfg, conn, dry_run=False)

    assert wiki_calls == []
    conn.close()


def test_salon_flow_insert_failure_then_next_round_skips(monkeypatch, caplog) -> None:
    """#324：INSERT 失败但当轮仍写 last_status → 下轮跳过，不重建 Wiki。"""
    from feedkicker import salon_flow as sf

    cfg = _sf_cfg(monkeypatch)
    conn = store.connect(":memory:")
    conn.execute("CREATE TRIGGER boom BEFORE INSERT ON articles BEGIN SELECT RAISE(ABORT, 'boom'); END;")
    conn.commit()
    monkeypatch.setattr(
        sf,
        "fetch_selected_topics",
        lambda *a, **kw: [{"record_id": "recT", "fields": {"讨论状态": ["已选题"], "话题名称": "触发题"}}],
    )
    monkeypatch.setattr(sf.minimax, "gen_outline", lambda *a, **kw: {"title": "t", "slides": [{"heading": "h", "bullets": ["a"]}]})
    wiki_calls: list[int] = []
    monkeypatch.setattr(sf.wiki, "create_wiki_doc_from_md", lambda *a, **kw: (wiki_calls.append(1), "https://wiki/w1")[1])
    monkeypatch.setattr(sf.salon_notify, "send_wiki_card", lambda *a, **kw: True)
    monkeypatch.setattr(sf.wiki_home, "update_homepage", lambda *a, **kw: True)

    with caplog.at_level(logging.ERROR):
        sf.run(cfg, conn, dry_run=False)

    assert len(wiki_calls) == 1
    assert store.get_ppt_last_status(conn, "recT") == "已选题"
    assert store.is_ppt_synced(conn, "recT") is False

    wiki_calls.clear()
    sf.run(cfg, conn, dry_run=False)
    assert wiki_calls == []
    conn.close()


def _sf_cfg(monkeypatch):
    monkeypatch.delenv("MiniMax_Key", raising=False)
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    cfg = load_cfg(app_env="test")
    cfg.salon.enabled = True
    cfg.salon.app_token = "app"
    cfg.salon.table_id = "tbl"
    cfg.salon.wiki_space_id = "spc"
    cfg.salon.wiki_parent_token = "parent"
    cfg.wiki.space_id = "spc"
    cfg.wiki.parent_token = "parent"
    cfg.wiki.app_token = "app_wiki"
    cfg.minimax.api_key = "sk"
    cfg.feishu_webhook = "https://hook.test"
    return cfg


# ── #325 推送/归档/提炼统一去重键（tracking 口径一致） ──


def test_dedup_key_strips_tracking_consistently() -> None:
    a = fetch.dedup_key("https://e.com/post?utm_source=rss")
    b = fetch.dedup_key("https://e.com/post?utm_source=weibo")
    assert a == b == "https://e.com/post"


def test_build_card_dedupes_same_url_different_utm() -> None:
    items = [
        _item("B源", "b1", "B转载", "https://e.com/post?utm_source=weibo"),
        _item("A源", "a1", "A原创", "https://e.com/post?utm_source=rss"),
    ]
    card = build_card(items, 0, ["A源", "B源"])
    content = card["card"]["elements"][0]["text"]["content"]

    assert content.count("https://e.com/post") == 1
    assert "A原创" in content
    assert "亦见 B源" in content


def test_build_card_keeps_tracking_distinct_hosts() -> None:
    items = [
        _item("A源", "a1", "A", "https://e.com/a?utm_source=rss"),
        _item("B源", "b1", "B", "https://e.com/b?utm_source=rss"),
    ]
    content = build_card(items, 0, ["A源", "B源"])["card"]["elements"][0]["text"]["content"]
    assert content.count("https://e.com/a") == 1
    assert content.count("https://e.com/b") == 1


# ── #326 topic rc0 不可识别响应必须 raise，合法空页仍返回 [] ──


@pytest.mark.parametrize("bad", [{}, {"x": 1}, {"ok": True, "data": {}}, {"ok": True}])
def test_extract_records_unrecognized_dict_raises(bad) -> None:
    with pytest.raises(RuntimeError):
        topic_mod._extract_records(bad)


@pytest.mark.parametrize("empty", [{"records": []}, {"items": []}, {"fields": ["标题"], "data": []}])
def test_extract_records_explicit_empty_returns_empty(empty) -> None:
    assert topic_mod._extract_records(empty) == []


@pytest.mark.parametrize(
    ("stdout", "rc"),
    [("", 0), ("help text / v9 banner", 0), ("[]", 0), ('{"ok": true, "data": {}}', 0)],
)
def test_fetch_selected_topics_unrecognized_raises(monkeypatch, stdout: str, rc: int) -> None:
    monkeypatch.setattr(
        topic_mod.bitable_lark, "_run", lambda *a, **kw: FakeProc(rc, stdout=stdout)
    )
    with pytest.raises(RuntimeError):
        topic_mod.fetch_selected_topics("app", "tbl")


def test_fetch_selected_topics_explicit_empty_page_ok(monkeypatch) -> None:
    monkeypatch.setattr(
        topic_mod.bitable_lark,
        "_run",
        lambda *a, **kw: FakeProc(0, stdout=json.dumps({"data": {"records": []}})),
    )
    assert topic_mod.fetch_selected_topics("app", "tbl") == []


def test_salon_flow_fetch_error_not_silent_rc0(monkeypatch) -> None:
    from feedkicker import salon_flow as sf

    cfg = _sf_cfg(monkeypatch)
    conn = store.connect(":memory:")
    monkeypatch.setattr(
        sf, "fetch_selected_topics", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("不可识别响应"))
    )
    with pytest.raises(RuntimeError):
        sf.run(cfg, conn, dry_run=False)
    conn.close()


# ── #327 嵌套未知键（providers.<name> / feeds[i]）告警且不阻断加载 ──


def test_nested_unknown_provider_and_feed_keys_warn(tmp_path, monkeypatch, caplog) -> None:
    monkeypatch.delenv("MiniMax_Key", raising=False)
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    path = tmp_path / "cfg.yaml"
    path.write_text(
        "feeds:\n"
        "  - name: F\n"
        "    url: https://e.com/rss\n"
        "    urll: https://typo/rss\n"
        "extract:\n"
        "  providers:\n"
        "    minimax:\n"
        "      base_url: https://api.minimaxi.com/v1\n"
        "      base_uri: TYPO\n"
        "      modell: TYPO\n"
        "      api-key: TYPO\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        cfg = load_cfg(config_path=path, app_env="test")

    msgs = [r.getMessage() for r in caplog.records]
    assert cfg.feeds[0].url == "https://e.com/rss"
    assert any("extract.providers.minimax.base_uri" in m for m in msgs)
    assert any("extract.providers.minimax.modell" in m for m in msgs)
    assert any("extract.providers.minimax.api-key" in m for m in msgs)
    assert any("feeds[0].urll" in m for m in msgs)


# ── #328 canonicalize 不抛 + parse_content 逐条隔离 ──


@pytest.mark.parametrize("bad", ["http://[", "http://[::1", "http://[fe80::1%25eth0]/"])
def test_canonicalize_invalid_url_does_not_raise(bad: str) -> None:
    assert canonicalize(bad) == bad


BAD_LINK_FEED = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<rss version="2.0"><channel><title>t</title>'
    b"<item><title>good1</title><link>https://good.com/1</link><guid>g1</guid></item>"
    b"<item><title>bad</title><link>http://[</link><guid>g2</guid></item>"
    b"<item><title>good2</title><link>https://good.com/2</link><guid>g3</guid></item>"
    b"</channel></rss>"
)


def test_parse_content_skips_bad_entry_and_keeps_rest(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        entries = fetch.parse_content(BAD_LINK_FEED)

    assert [e["title"] for e in entries] == ["good1", "good2"]
    assert any("跳过" in r.getMessage() or "畸形" in r.getMessage() for r in caplog.records)


def test_build_card_poisoned_url_does_not_raise() -> None:
    items = [_item("F", "k", "t", "http://[")]
    assert build_card(items, 0, ["F"]) is not None


# ── #329 非 URL token 不产生去重键，不同 URL 同注记均写入 ──


def test_link_keys_ignores_prose_token() -> None:
    assert link_keys("详见 [原文](https://x.com/1)") == {"https://x.com/1"}


def test_link_keys_ignores_quoted_link_title() -> None:
    assert link_keys('[a](https://e.com/1 "来源")') == {"https://e.com/1"}


def test_md_link_tokens_filters_non_url_tokens() -> None:
    assert md_link_tokens("详见 [原文](https://x.com/1)") == ["https://x.com/1"]


def test_plan_writes_writes_distinct_urls_sharing_annotation() -> None:
    existing = link_keys("[A](https://a.com/1)（量子位）")
    topics = [
        {"话题名称": "B", "资讯链接": ["[B](https://b.com/2)（量子位）"]},
        {"话题名称": "C", "资讯链接": ["[C](https://c.com/3)（量子位）"]},
    ]

    picked, skipped = plan_writes(topics, "DS", "2026-09-15", set(), existing)

    assert [p["话题名称"] for p in picked] == ["B", "C"]
    assert skipped == []
