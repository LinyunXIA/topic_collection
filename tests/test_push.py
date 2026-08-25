from __future__ import annotations

import base64
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from feedkicker import feishu, push, store
from feedkicker.config import Config, Feed, HttpConf, SiteConf, load_config
from feedkicker.fetch import canonicalize, entry_key_of, parse_content


def rss(*items: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<rss version=\"2.0\"><channel><title>T</title><link>https://e.com</link>"
        + "".join(items)
        + "</channel></rss>"
    ).encode("utf-8")


def item(
    title: str,
    link: str,
    *,
    guid: str | None = None,
    desc: str | None = None,
    pubdate: str | None = None,
) -> str:
    parts = [f"<title>{title}</title>", f"<link>{link}</link>"]
    if guid:
        parts.append(f"<guid>{guid}</guid>")
    if desc:
        parts.append(f"<description>{desc}</description>")
    if pubdate:
        parts.append(f"<pubDate>{pubdate}</pubDate>")
    return "<item>" + "".join(parts) + "</item>"


def days_ago(n: int) -> str:
    return format_datetime(datetime.now(timezone.utc) - timedelta(days=n))


def make_conn() -> sqlite3.Connection:
    return store.connect(":memory:")


def make_cfg(feeds: list[Feed], **site_kwargs) -> Config:
    return Config(
        feishu_webhook="",
        bootstrap_days=3,
        http=HttpConf(timeout_seconds=5),
        feeds=feeds,
        site=SiteConf(enabled=False, **site_kwargs),
    )


# ── fetch 归一化 ──


def test_fetch_normalizes():
    entries = parse_content(
        rss(
            item(
                "Hello World",
                "https://Example.com/posts/1?utm=x#top",
                guid="guid-001",
                desc="原始摘要文本",
                pubdate="Mon, 24 Aug 2026 08:30:00 GMT",
            )
        )
    )
    assert len(entries) == 1
    e = entries[0]
    assert e["entry_key"] == "guid-001"
    assert e["title"] == "Hello World"
    assert e["url"] == "https://example.com/posts/1?utm=x"
    assert e["description"] == "原始摘要文本"
    assert e["published_at"] == "2026-08-24T08:30:00Z"


def test_guid_vs_link_key():
    with_guid = parse_content(
        rss(item("A", "https://example.com/a#frag", guid="abc-123"))
    )[0]
    assert with_guid["entry_key"] == "abc-123"

    no_guid = parse_content(rss(item("B", "https://Example.com/b?x=1#sec")))[0]
    assert no_guid["entry_key"] == "https://example.com/b?x=1"

    bare = parse_content(rss("<item><title>Duplicate Title</title></item>"))[0]
    assert bare["entry_key"] == "title:duplicate title"

    assert canonicalize("https://EXAMPLE.com/p?x=1#frag") == "https://example.com/p?x=1"
    assert "?" in canonicalize("https://e.com/p?a=1&b=2")


def test_parse_rejects_empty_and_bad():
    with pytest.raises(ValueError):
        parse_content(rss())
    with pytest.raises(ValueError):
        parse_content(b"this is not xml at all")


# ── store ──


SAMPLE_ENTRIES = [
    {
        "entry_key": "k1",
        "title": "t1",
        "url": "https://e.com/1",
        "description": "",
        "published_at": None,
    },
    {
        "entry_key": "k2",
        "title": "t2",
        "url": "https://e.com/2",
        "description": "d2",
        "published_at": "2026-08-20T00:00:00Z",
    },
]


def test_download_idempotent():
    conn = make_conn()
    n1 = store.download(conn, "F", SAMPLE_ENTRIES, "2026-08-25T00:00:00Z")
    assert n1 == 2
    n2 = store.download(conn, "F", SAMPLE_ENTRIES, "2026-08-25T01:00:00Z")
    assert n2 == 0
    assert len(store.select_pending(conn)) == 2
    conn.close()


def test_bootstrap_window():
    conn = make_conn()
    entries = parse_content(
        rss(
            item("old post", "https://e.com/old", desc="o", pubdate=days_ago(5)),
            item("new post", "https://e.com/new", desc="n", pubdate=days_ago(1)),
            item("no date post", "https://e.com/nodate"),
        )
    )
    now = "2026-08-25T12:00:00Z"
    cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    store.download(conn, "F", entries, now)
    assert store.is_first_run(conn, "F")
    skipped = store.promise_skip_old(conn, "F", cutoff, now)
    assert skipped >= 1
    pending_urls = {e["url"] for e in store.select_pending(conn)}
    assert pending_urls == {"https://e.com/new", "https://e.com/nodate"}
    conn.close()


def test_null_published_not_excluded():
    conn = make_conn()
    entries = parse_content(rss(item("no date", "https://e.com/x")))
    now = "2026-08-25T00:00:00Z"
    store.download(conn, "F", entries, now)
    store.promise_skip_old(conn, "F", "2026-08-22T00:00:00Z", now)
    pending = store.select_pending(conn)
    assert [e["url"] for e in pending] == ["https://e.com/x"]
    conn.close()


def test_second_run_after_bootstrap():
    conn = make_conn()
    cfg = make_cfg([Feed(name="F", url="https://e.com/rss")])
    now = "2026-08-25T00:00:00Z"
    first = parse_content(
        rss(item("old", "https://e.com/o", pubdate=days_ago(10)))
    )
    store.download(conn, "F", first, now)
    store.promise_skip_old(
        conn,
        "F",
        (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        now,
    )
    store.update_first_run_all(conn, cfg.feeds, now)
    assert not store.is_first_run(conn, "F")

    later = parse_content(rss(item("still old", "https://e.com/s", pubdate=days_ago(10))))
    store.download(conn, "F", later, now)
    pending_urls = {e["url"] for e in store.select_pending(conn)}
    assert "https://e.com/s" in pending_urls
    conn.close()


# ── 飞书卡片 ──


CARD_ITEMS = [
    {
        "feed_id": "B 源",
        "entry_key": "b1",
        "title": "B 文章",
        "url": "https://b.com/1",
        "description": "第一行\n第二行",
    },
    {
        "feed_id": "A 源",
        "entry_key": "a1",
        "title": "A_Article",
        "url": "https://a.com/1",
        "description": "**Bold** [link](x)",
    },
    {
        "feed_id": "A 源",
        "entry_key": "a2",
        "title": "无摘要",
        "url": "https://a.com/2",
        "description": "",
    },
]


def test_build_card_grouping():
    card = feishu.build_card(CARD_ITEMS, 0, ["A 源", "B 源"])
    assert card["msg_type"] == "interactive"
    assert card["card"]["header"]["template"] == "blue"
    elements = card["card"]["elements"]
    assert len(elements) == 1
    content = elements[0]["text"]["content"]
    expected = (
        "**A 源**\n"
        "[A\\_Article](https://a.com/1)\n"
        "\\*\\*Bold\\*\\* \\[link\\]\\(x\\)\n"
        "[无摘要](https://a.com/2)\n"
        "\n"
        "**B 源**\n"
        "[B 文章](https://b.com/1)\n"
        "第一行 第二行"
    )
    assert content == expected


def test_build_card_failure_footer():
    card_ok = feishu.build_card(CARD_ITEMS, 0, ["A 源"])
    assert all(el.get("tag") != "hr" for el in card_ok["card"]["elements"])

    card_fail = feishu.build_card(CARD_ITEMS, 2, ["A 源"])
    elements = card_fail["card"]["elements"]
    assert elements[-2] == {"tag": "hr"}
    assert elements[-1]["text"]["content"] == "⚠ 2 个源失败"


def test_escape_inline():
    assert feishu.escape_inline("a*b`c[d](e)#f_g\\h") == "a\\*b\\`c\\[d\\]\\(e\\)\\#f\\_g\\\\h"
    assert feishu.escape_inline("x\r\ny") == "x y"
    assert feishu.escape_inline(None) == ""


def test_gen_sign_known_vector():
    assert (
        feishu.gen_sign("1599360473", "demo")
        == "l1N0gAcBjdwBvGm1xMjOF0XSyaLRpR7tuO5dHfhAYc8="
    )
    sign = feishu.gen_sign("1600000000", "s")
    assert len(sign) == 44
    base64.b64decode(sign)


def test_send_injects_signature(monkeypatch):
    captured = []

    def fake_post(url, content=None, **kw):
        captured.append(json.loads(content.decode("utf-8")))
        return FakeResp({"code": 0})

    monkeypatch.setattr(feishu.httpx, "post", fake_post)
    payload = {"msg_type": "text"}
    assert feishu.send(payload, "https://hook.test/x", 5, "ua", secret="topsecret")
    assert len(captured) == 1
    sent = captured[0]
    assert sent["timestamp"].isdigit() and len(sent["timestamp"]) == 10
    assert sent["sign"] == feishu.gen_sign(sent["timestamp"], "topsecret")

    payload2 = {"msg_type": "text"}
    assert feishu.send(payload2, "https://hook.test/x", 5, "ua", secret="")
    assert "sign" not in captured[1] and "timestamp" not in captured[1]


def test_build_card_trims_to_20kb():
    big_items = [
        {
            "feed_id": "F",
            "entry_key": f"k{i}",
            "title": f"标题 {i} " + "很长的摘要" * 60,
            "url": f"https://e.com/{i}",
            "description": "描述行 " * 120,
        }
        for i in range(40)
    ]
    card = feishu.build_card(big_items, 0, ["F"])
    raw = json.dumps(card, ensure_ascii=False)
    assert len(raw.encode("utf-8")) <= 20000
    assert card["msg_type"] == "interactive"
    texts = [el.get("text", {}).get("content", "") for el in card["card"]["elements"]]
    assert any("已截断" in t for t in texts)

    small = big_items[:2]
    card_small = feishu.build_card(small, 0, ["F"])
    small_texts = [
        el.get("text", {}).get("content", "") for el in card_small["card"]["elements"]
    ]
    assert not any("已截断" in t for t in small_texts)
    assert "描述行" in small_texts[0]
    assert len(json.dumps(card_small, ensure_ascii=False).encode("utf-8")) < 20000


class FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def test_send_business_code(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return FakeResp({"StatusCode": 0})

    monkeypatch.setattr(feishu.httpx, "post", fake_post)
    assert feishu.send({"msg_type": "text"}, "https://hook.test/x", 5, "ua") is True

    monkeypatch.setattr(
        feishu.httpx, "post", lambda url, **kw: FakeResp({"StatusCode": 1})
    )
    assert feishu.send({}, "https://hook.test/x", 5, "ua") is False

    monkeypatch.setattr(feishu.httpx, "post", lambda url, **kw: FakeResp({"code": 0}))
    assert feishu.send({}, "https://hook.test/x", 5, "ua") is True

    assert feishu.send({}, "", 5, "ua") is False
    assert len(calls) == 1


# ── push 编排 ──


def _norm(title, link):
    return parse_content(rss(item(title, link)))[0]


def test_no_new_items_no_empty_card(monkeypatch):
    conn = make_conn()
    cfg = make_cfg([Feed(name="F", url="https://e.com/rss")])
    entries = [_norm("already seen", "https://e.com/dup")]
    now = "2026-08-25T00:00:00Z"
    store.download(conn, "F", entries, now)
    store.mark_pushed(conn, store.select_pending(conn), now)

    monkeypatch.setattr(push, "fetch_feed", lambda url, http: entries)
    called = []
    monkeypatch.setattr(feishu, "build_card", lambda *a, **kw: called.append(a))

    rc = push.run(cfg, conn)
    assert rc == 0
    assert not called
    assert not store.is_first_run(conn, "F")
    conn.close()


def test_run_pushes_marks_and_dedupes(monkeypatch):
    conn = make_conn()
    cfg = make_cfg([Feed(name="F", url="https://e.com/rss")])
    entries = [_norm("fresh", "https://e.com/fresh")]
    monkeypatch.setattr(push, "fetch_feed", lambda url, http: entries)

    sent = []
    monkeypatch.setattr(
        feishu,
        "send",
        lambda payload, hook, timeout, ua, **kw: sent.append(payload) or (hook == "hook-x"),
    )

    cfg.feishu_webhook = "hook-x"
    rc1 = push.run(cfg, conn)
    assert rc1 == 0
    assert len(sent) == 1
    assert store.select_pending(conn) == []

    rc2 = push.run(cfg, conn)
    assert rc2 == 0
    assert len(sent) == 1
    conn.close()


def test_run_dry_run_prints_not_sends(monkeypatch, capsys):
    conn = make_conn()
    cfg = make_cfg([Feed(name="F", url="https://e.com/rss")])
    entries = [_norm("dry", "https://e.com/dry")]
    monkeypatch.setattr(push, "fetch_feed", lambda url, http: entries)

    sent = []
    monkeypatch.setattr(feishu, "send", lambda *a: sent.append(a))

    rc = push.run(cfg, conn, dry_run=True)
    assert rc == 0
    out = capsys.readouterr().out
    assert '"msg_type": "interactive"' in out
    assert "https://e.com/dry" in out
    assert not sent
    assert len(store.select_pending(conn)) == 1
    conn.close()


def test_run_one_source_fails_others_push(monkeypatch):
    def fake_fetch(url, http):
        if "bad" in url:
            raise ValueError("boom")
        return [_norm("good one", "https://good.com/1")]

    conn = make_conn()
    cfg = make_cfg(
        [
            Feed(name="坏源", url="https://bad.example/rss"),
            Feed(name="好源", url="https://good.example/rss"),
        ]
    )
    monkeypatch.setattr(push, "fetch_feed", fake_fetch)
    monkeypatch.setattr(feishu, "send", lambda *a, **kw: True)

    rc = push.run(cfg, conn)
    assert rc == 0
    row = conn.execute(
        "SELECT fail_streak FROM feeds WHERE feed_id = '坏源'"
    ).fetchone()
    assert row[0] == 1
    ok_row = conn.execute(
        "SELECT fail_streak FROM feeds WHERE feed_id = '好源'"
    ).fetchone()
    assert ok_row[0] == 0
    conn.close()


def test_main_missing_config_returns_error():
    rc = push.main(["--config", "/nonexistent/config.yaml"])
    assert rc == 2


def test_entry_key_of_takes_guid():
    class E(dict):
        pass

    assert entry_key_of({"id": " g1 ", "link": "https://x.com/a"}) == "g1"


def _many_items(feed, n):
    return [
        {"feed_id": feed, "entry_key": f"k{i}", "title": f"标题{i}",
         "url": f"https://e.com/{i}", "description": f"摘要{i}", "published_at": None}
        for i in range(n)
    ]


def test_build_card_top_n_and_button():
    card = feishu.build_card(_many_items("F", 8), 0, ["F"], top_n=3,
                             detail_url="https://linyunxia.github.io/topic_collection/daily/2026-08-25.html")
    content = card["card"]["elements"][0]["text"]["content"]
    assert "标题7" not in content and "标题2" in content
    assert "还有 5 条，详情见在线表格" in content
    actions = [el for el in card["card"]["elements"] if el["tag"] == "action"]
    assert actions and actions[0]["actions"][0]["text"]["content"] == "📰 详情见在线表格"
    assert actions[0]["actions"][0]["url"].endswith("2026-08-25.html")
    stripped = feishu.strip_actions(card)
    assert all(el["tag"] != "action" for el in stripped["card"]["elements"])
    assert any("详情见在线表格" in el.get("text", {}).get("content", "")
               for el in stripped["card"]["elements"] if el.get("tag") == "div")


def test_build_card_no_detail_no_button():
    card = feishu.build_card(_many_items("F", 4), 0, ["F"], top_n=3)
    assert all(el["tag"] != "action" for el in card["card"]["elements"])
    assert not any("[详情见在线表格](" in el.get("text", {}).get("content", "")
                   for el in card["card"]["elements"] if el.get("tag") == "div")


# ── v0.2：编排顺序与降级 ──


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ── v0.5：多维表格编排 ──


def test_push_bitable_sync_before_send(monkeypatch):
    conn = make_conn()
    cfg = make_cfg([Feed(name="F", url="https://e.com/rss")])
    cfg.bitable.enabled = True
    cfg.bitable.app_token = "app-x"
    cfg.bitable.url = "https://x.test/base"
    cfg.feishu_webhook = "hook-x"
    entries = [_norm("bt item", "https://e.com/ar1")]

    calls = []
    monkeypatch.setattr(push, "fetch_feed", lambda u, h: entries)
    monkeypatch.setattr(push.bitable, "sync_env",
                        lambda b, e, c: calls.append(("sync",)) or 1)
    sent = []
    monkeypatch.setattr(feishu, "send",
                        lambda p, *a, **kw: calls.append(("send",)) or sent.append(p) or True)

    rc = push.run(cfg, conn)
    assert rc == 0
    assert [c[0] for c in calls] == ["sync", "send"]
    actions = [el for el in sent[0]["card"]["elements"] if el.get("tag") == "action"]
    assert actions and "详情见多维表格" in actions[0]["actions"][0]["text"]["content"]
    assert store.select_pending(conn) == []
    conn.close()


def test_push_bitable_fail_still_sends(monkeypatch):
    conn = make_conn()
    cfg = make_cfg([Feed(name="F", url="https://e.com/rss")])
    cfg.bitable.enabled = True
    cfg.bitable.app_token = "app-x"
    cfg.feishu_webhook = "hook-x"
    entries = [_norm("degrade", "https://e.com/dg1")]
    monkeypatch.setattr(push, "fetch_feed", lambda u, h: entries)

    def boom(b, e, c):
        raise RuntimeError("写入失败")

    monkeypatch.setattr(push.bitable, "sync_env", boom)

    sent = []
    monkeypatch.setattr(feishu, "send",
                        lambda p, *a, **kw: sent.append(p) or True)
    rc = push.run(cfg, conn)
    assert rc == 0
    assert len(sent) == 1
    actions = [el for el in sent[0]["card"]["elements"] if el.get("tag") == "action"]
    assert actions and "app-x" in actions[0]["actions"][0]["url"]
    assert len(store.select_unsynced(conn)) == 1
    conn.close()


def test_run_sos_after_three_failures(monkeypatch):
    conn = make_conn()
    cfg = make_cfg([Feed(name="F", url="https://e.com/rss")])
    cfg.feishu_webhook = "hook-x"
    entries = [_norm("sos item", "https://e.com/s1")]
    monkeypatch.setattr(push, "fetch_feed", lambda u, h: entries)
    monkeypatch.setattr(feishu, "send", lambda *a, **kw: False)

    sos_calls = []
    monkeypatch.setattr(feishu, "send_text",
                        lambda msg, *a, **kw: sos_calls.append(msg) or True)

    store.set_meta(conn, push.PUSH_FAIL_STREAK_KEY, "2")
    rc = push.run(cfg, conn)
    assert rc == 1
    assert len(sos_calls) == 1 and "连续 3 次" in sos_calls[0]
    assert store.get_meta(conn, push.PUSH_FAIL_STREAK_KEY) == "0"
    conn.close()


def test_store_sync_roundtrip():
    conn = make_conn()
    store.download(conn, "F", SAMPLE_ENTRIES, "2026-08-25T00:00:00Z")
    assert len(store.select_unsynced(conn)) == 2
    store.mark_synced(conn, store.select_unsynced(conn), "2026-08-25T01:00:00Z")
    assert store.select_unsynced(conn) == []
    conn.close()


def test_bitable_dedup_filters(monkeypatch):
    from feedkicker import bitable

    monkeypatch.setattr(bitable, "existing_links",
                        lambda a, t: {"https://old.com/1"})
    calls = []

    def fake_run(args, stdin_text=None, timeout=120):
        if "+record-batch-create" in args:
            payload = json.loads(args[args.index("--json") + 1])
            calls.append(len(payload["create_records"]))
            return FakeProc(0, stdout="{}")
        return FakeProc(0, stdout="{}")

    monkeypatch.setattr(bitable, "_run", fake_run)

    items = (
        [{"feed_id": f"F{i}", "entry_key": f"k{i}", "title": f"t{i}",
          "url": f"https://e.com/{i}", "description": "", "published_at": None,
          "pushed_at": None} for i in range(250)]
        + [{"feed_id": "F", "entry_key": "dup", "title": "dup",
            "url": "HTTPS://E.COM/1#x", "description": "", "published_at": None,
            "pushed_at": None}]
        + [{"feed_id": "F", "entry_key": "old", "title": "old",
            "url": "https://old.com/1", "description": "", "published_at": None,
            "pushed_at": None}]
    )
    assert bitable.sync_records("app", "tbl", items, env_name="dev") is True
    assert calls == [200, 50]


def test_bitable_cell_fields(monkeypatch):
    from feedkicker import bitable

    cell = bitable._cell({
        "feed_id": "量子位", "title": "标题", "url": "https://e.com/1",
        "description": "", "published_at": "2026-08-25T01:30:00Z",
        "pushed_at": "2026-08-25T09:59:53Z",
    }, env_name="dev")
    assert cell["归档日期"] == "2026-08-25" or len(cell["归档日期"]) == 10
    assert cell["环境"] == "dev"
    assert len(cell["推送时间"]) == 16

    prod_cell = bitable._cell({
        "feed_id": "量子位", "title": "t", "url": "u",
        "description": "", "published_at": None, "pushed_at": None,
    })
    assert "环境" not in prod_cell
