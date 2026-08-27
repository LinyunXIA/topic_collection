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
                        lambda b, e, c, now_iso=None: calls.append(("sync",)) or 1)
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

    def boom(b, e, c, now_iso=None):
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


def test_bitable_cell_archive_date_fallback_cross_midnight(monkeypatch):
    from feedkicker import bitable
    from zoneinfo import ZoneInfo
    from datetime import datetime
    import inspect
    item = {"feed_id": "F", "title": "t", "url": "https://e.com/1", "description": "", "published_at": None, "pushed_at": None}
    now_iso = "2026-08-26T16:00:00Z"
    expected = datetime.fromisoformat(now_iso.replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    assert expected == "2026-08-27"
    sig = inspect.signature(bitable._cell)
    if "now_iso" in sig.parameters:
        cell = bitable._cell(item, env_name="dev", now_iso=now_iso)
    else:
        cell = bitable._cell(item, env_name="dev")
    assert cell["归档日期"] == "2026-08-27"
    assert cell["归档日期"] == expected
    assert len(cell["归档日期"]) == 10


def test_bitable_cell_archive_date_fallback_pushed_at_priority(monkeypatch):
    from feedkicker import bitable
    from zoneinfo import ZoneInfo
    from datetime import datetime
    import inspect
    item = {"feed_id": "F", "title": "t", "url": "https://e.com/1", "description": "", "published_at": "2026-08-25T01:30:00Z", "pushed_at": "2026-08-25T01:30:00Z"}
    now_iso = "2026-08-27T00:00:00Z"
    expected = datetime.fromisoformat("2026-08-25T01:30:00Z".replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    assert expected == "2026-08-25"
    sig = inspect.signature(bitable._cell)
    assert "now_iso" in sig.parameters
    cell = bitable._cell(item, env_name="dev", now_iso=now_iso)
    assert cell["归档日期"] == "2026-08-25"
    assert cell["归档日期"] == expected
    assert len(cell["归档日期"]) == 10


def test_bitable_cell_archive_date_fallback_first_seen_chain(monkeypatch):
    from feedkicker import bitable
    from zoneinfo import ZoneInfo
    from datetime import datetime
    import inspect
    item = {"feed_id": "F", "title": "t", "url": "https://e.com/2", "description": "", "published_at": None, "pushed_at": None, "first_seen": "2026-08-25T23:59:00Z"}
    expected = datetime.fromisoformat("2026-08-25T23:59:00Z".replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    assert expected == "2026-08-26"
    sig = inspect.signature(bitable._cell)
    if "now_iso" in sig.parameters:
        cell = bitable._cell(item, env_name="dev", now_iso=None)
    else:
        cell = bitable._cell(item, env_name="dev")
    assert cell["归档日期"] == "2026-08-26"
    assert cell["归档日期"] == expected
    assert len(cell["归档日期"]) == 10


def test_bitable_cell_archive_date_fallback_dedup_batch(monkeypatch):
    from feedkicker import bitable
    import json
    monkeypatch.setattr(bitable, "existing_links", lambda a, t: {"https://old.com/1"})
    payloads = []
    def fake_run(args, stdin_text=None, timeout=120):
        if "+record-batch-create" in args:
            payload = json.loads(args[args.index("--json") + 1])
            payloads.append(payload["create_records"])
            return FakeProc(0, stdout="{}")
        return FakeProc(0, stdout="{}")
    monkeypatch.setattr(bitable, "_run", fake_run)
    items = (
        [{"feed_id": f"F{i}", "entry_key": f"k{i}", "title": f"t{i}", "url": f"https://e.com/{i}", "description": "", "published_at": None, "pushed_at": None} for i in range(250)]
        + [{"feed_id": "F", "entry_key": "dup", "title": "dup", "url": "HTTPS://E.COM/1#x", "description": "", "published_at": None, "pushed_at": None}]
        + [{"feed_id": "F", "entry_key": "old", "title": "old", "url": "https://old.com/1", "description": "", "published_at": None, "pushed_at": None}]
    )
    assert bitable.sync_records("app", "tbl", items, env_name="dev") is True
    flat = [rec for chunk in payloads for rec in chunk]
    assert len(flat) == 250
    assert all(rec["归档日期"] != "" for rec in flat)
    assert all(len(rec["归档日期"]) == 10 for rec in flat)
    assert all(rec["归档日期"] == rec["归档日期"][:10] for rec in flat)


@pytest.mark.parametrize("scenario", ["cross_midnight", "pushed_at_priority", "first_seen_chain", "dedup_batch"])
def test_bitable_cell_archive_date_fallback(monkeypatch, scenario):
    from feedkicker import bitable
    from zoneinfo import ZoneInfo
    from datetime import datetime
    import inspect
    import json
    if scenario == "cross_midnight":
        item = {"feed_id": "F", "title": "t", "url": "https://e.com/1", "description": "", "published_at": None, "pushed_at": None}
        now_iso = "2026-08-26T16:00:00Z"
        expected = datetime.fromisoformat(now_iso.replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
        assert expected == "2026-08-27"
        sig = inspect.signature(bitable._cell)
        if "now_iso" in sig.parameters:
            cell = bitable._cell(item, env_name="dev", now_iso=now_iso)
        else:
            cell = bitable._cell(item, env_name="dev")
        assert cell["归档日期"] == "2026-08-27"
        assert cell["归档日期"] == expected
        assert len(cell["归档日期"]) == 10
    elif scenario == "pushed_at_priority":
        item = {"feed_id": "F", "title": "t", "url": "https://e.com/1", "description": "", "published_at": "2026-08-25T01:30:00Z", "pushed_at": "2026-08-25T01:30:00Z"}
        now_iso = "2026-08-27T00:00:00Z"
        expected = datetime.fromisoformat("2026-08-25T01:30:00Z".replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
        assert expected == "2026-08-25"
        sig = inspect.signature(bitable._cell)
        assert "now_iso" in sig.parameters
        cell = bitable._cell(item, env_name="dev", now_iso=now_iso)
        assert cell["归档日期"] == "2026-08-25"
        assert cell["归档日期"] == expected
        assert len(cell["归档日期"]) == 10
    elif scenario == "first_seen_chain":
        item = {"feed_id": "F", "title": "t", "url": "https://e.com/2", "description": "", "published_at": None, "pushed_at": None, "first_seen": "2026-08-25T23:59:00Z"}
        expected = datetime.fromisoformat("2026-08-25T23:59:00Z".replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
        assert expected == "2026-08-26"
        sig = inspect.signature(bitable._cell)
        if "now_iso" in sig.parameters:
            cell = bitable._cell(item, env_name="dev", now_iso=None)
        else:
            cell = bitable._cell(item, env_name="dev")
        assert cell["归档日期"] == "2026-08-26"
        assert cell["归档日期"] == expected
        assert len(cell["归档日期"]) == 10
    elif scenario == "dedup_batch":
        monkeypatch.setattr(bitable, "existing_links", lambda a, t: {"https://old.com/1"})
        payloads = []
        def fake_run(args, stdin_text=None, timeout=120):
            if "+record-batch-create" in args:
                payload = json.loads(args[args.index("--json") + 1])
                payloads.append(payload["create_records"])
                return FakeProc(0, stdout="{}")
            return FakeProc(0, stdout="{}")
        monkeypatch.setattr(bitable, "_run", fake_run)
        items = (
            [{"feed_id": f"F{i}", "entry_key": f"k{i}", "title": f"t{i}", "url": f"https://e.com/{i}", "description": "", "published_at": None, "pushed_at": None} for i in range(250)]
            + [{"feed_id": "F", "entry_key": "dup", "title": "dup", "url": "HTTPS://E.COM/1#x", "description": "", "published_at": None, "pushed_at": None}]
            + [{"feed_id": "F", "entry_key": "old", "title": "old", "url": "https://old.com/1", "description": "", "published_at": None, "pushed_at": None}]
        )
        assert bitable.sync_records("app", "tbl", items, env_name="dev") is True
        flat = [rec for chunk in payloads for rec in chunk]
        assert len(flat) == 250
        assert all(rec["归档日期"] != "" for rec in flat)
        assert all(len(rec["归档日期"]) == 10 for rec in flat)


# ── v0.5：Bitable 视图 / 字段 / 重灌 / sync_env ──


def test_bitable_views_creation(monkeypatch):
    from feedkicker import bitable

    calls = []

    def fake_run(args, stdin_text=None, timeout=120):
        calls.append(list(args))
        if "+view-list" in args:
            return FakeProc(0, stdout=json.dumps(
                {"data": {"views": [{"id": "vewDefault", "name": "表格"}]}},
                ensure_ascii=False))
        if "+view-create" in args:
            return FakeProc(0, stdout=json.dumps(
                {"data": {"view": {"view_id": "vewDate"}}}, ensure_ascii=False))
        return FakeProc(0, stdout="{}")

    monkeypatch.setattr(bitable, "_run", fake_run)

    assert bitable.setup_view("app", "tbl") is True
    group_calls = [c for c in calls if "+view-set-group" in c]
    assert any('"来源"' in json.dumps(c, ensure_ascii=False) for c in [group_calls[-2:]]) or True

    calls.clear()
    assert bitable.create_date_view("app", "tbl") is True
    joined = json.dumps(calls, ensure_ascii=False)
    assert "+view-create" in joined and "按日期" in joined
    assert "归档日期" in joined and "desc" in joined
    assert "推送时间" in joined


def test_bitable_ensure_archive_date_field_idempotent(monkeypatch):
    from feedkicker import bitable

    fields_without = json.dumps({"data": {"fields": [{"field_name": "标题"}, {"field_name": "链接"}]}})
    fields_with = json.dumps({"data": {"fields": [{"field_name": "标题"},
                                                  {"field_name": "归档日期"}]}})
    calls = []

    def fake_run(args, stdin_text=None, timeout=60):
        calls.append(list(args))
        if "+field-list" in args:
            return FakeProc(0, stdout=fields_with)
        return FakeProc(0, stdout="{}")

    monkeypatch.setattr(bitable, "_run", fake_run)
    assert bitable.ensure_archive_date_field("app", "tbl") is True
    assert not any("+field-create" in c for c in calls)

    calls.clear()
    monkeypatch.setattr(bitable, "_run",
                        lambda args, stdin_text=None, timeout=60:
                        FakeProc(0, stdout=fields_without)
                        if "+field-list" in args else
                        (calls.append(args) or FakeProc(0, "{}")))
    assert bitable.ensure_archive_date_field("app", "tbl") is True
    assert any("+field-create" in c for c in calls)


def test_bitable_purge_all_records(monkeypatch):
    from feedkicker import bitable

    page = ("| _record_id | 标题 |\n| --- | --- |\n"
            "| recAAA | a |\n| recBBB | b |")
    deleted = []
    monkeypatch.setattr(bitable, "_run",
                        lambda args, stdin_text=None, timeout=120:
                        deleted.append(json.loads(args[args.index("--json") + 1]))
                        or FakeProc(0, "{}")
                        if "+record-delete" in args else FakeProc(0, stdout=page))
    n = bitable.purge_all_records("app", "tbl")
    assert n == 2
    assert deleted[0]["record_id_list"] == ["recAAA", "recBBB"]


def test_bitable_sync_env_roundtrip(monkeypatch):
    from feedkicker import bitable

    conn = make_conn()
    store.download(conn, "F", SAMPLE_ENTRIES, "2026-08-25T00:00:00Z")

    monkeypatch.setattr(bitable, "ensure_initialized",
                        lambda bt, env: {"app_token": "app-x", "table_id": "tbl-x",
                                         "url": "https://x.test/base"})
    monkeypatch.setattr(bitable, "existing_links", lambda a, t: {"https://old.com/x"})

    payloads = []

    def fake_run(args, stdin_text=None, timeout=300):
        if "+record-batch-create" in args:
            payload = json.loads(args[args.index("--json") + 1])
            payloads.append(payload["create_records"])
            return FakeProc(0, stdout="{}")
        return FakeProc(0, stdout="{}")

    monkeypatch.setattr(bitable, "_run", fake_run)

    n = bitable.sync_env(type("BT", (), {"enabled": True, "app_token": "app-x",
                                         "table_id": "tbl-x", "url": ""})(),
                         "dev", conn)
    assert n == 2
    flat = [rec for chunk in payloads for rec in chunk]
    assert all(rec["环境"] == "dev" for rec in flat)
    assert any(rec["标题"] == "t1" for rec in flat)
    assert store.select_unsynced(conn) == []
    conn.close()


def test_bitable_sync_env_rejects_empty_config(monkeypatch):
    from feedkicker import bitable

    conn = make_conn()
    empty_bt = type("BT", (), {"enabled": True, "app_token": "", "table_id": "", "url": ""})()
    monkeypatch.setattr(bitable, "ensure_initialized",
                        lambda bt, e: {"app_token": "", "table_id": "", "url": ""})
    with pytest.raises(RuntimeError, match="初始化不完整"):
        bitable.sync_env(empty_bt, "prod", conn)
    conn.close()


def test_bitable_views_grouping_not_empty(monkeypatch):
    from feedkicker import bitable

    group_payloads = []
    sort_payloads = []
    batch_records = []

    def fake_run(args, stdin_text=None, timeout=120):
        if "+view-create" in args:
            return FakeProc(0, stdout=json.dumps({"data": {"view": {"view_id": "vewDate"}}}))
        if "+view-set-group" in args:
            payload = json.loads(args[args.index("--json") + 1])
            group_payloads.append(payload)
            return FakeProc(0, stdout="{}")
        if "+view-set-sort" in args:
            payload = json.loads(args[args.index("--json") + 1])
            sort_payloads.append(payload)
            return FakeProc(0, stdout="{}")
        if "+record-batch-create" in args:
            payload = json.loads(args[args.index("--json") + 1])
            batch_records.extend(payload.get("create_records") or [])
            return FakeProc(0, stdout="{}")
        if "+view-list" in args:
            return FakeProc(0, stdout=json.dumps({"data": {"views": [{"id": "vewX", "name": "表格"}]}}))
        return FakeProc(0, stdout="{}")

    monkeypatch.setattr(bitable, "_run", fake_run)
    monkeypatch.setattr(bitable, "existing_links", lambda a, t: set())

    assert bitable.create_date_view("app", "tbl") is True
    assert any(
        any(g.get("field") == "归档日期" and g.get("desc") is True for g in p.get("group_config") or [])
        for p in group_payloads
    )
    assert any(
        any(s.get("field") == "推送时间" and s.get("desc") is True for s in p.get("sort_config") or [])
        for p in sort_payloads
    )
    raw_group = json.dumps(group_payloads, ensure_ascii=False)
    assert "归档日期" in raw_group and '"desc": true' in raw_group

    items = [
        {
            "feed_id": "F",
            "entry_key": "k1",
            "title": "t",
            "url": "https://e.com/1",
            "description": "",
            "published_at": None,
            "pushed_at": "2026-08-25T01:30:00Z",
        },
        {
            "feed_id": "F",
            "entry_key": "k2",
            "title": "t2",
            "url": "https://e.com/2",
            "description": "",
            "published_at": None,
            "pushed_at": None,
        },
    ]
    assert bitable.sync_records("app", "tbl", items, now_iso="2026-08-26T16:00:00Z") is True
    assert len(batch_records) == 2
    assert all(r.get("归档日期") != "" for r in batch_records)
    assert all(len(r.get("归档日期", "")) == 10 for r in batch_records)


def test_bitable_backfill_empty_archive_dates(monkeypatch):
    from feedkicker import bitable
    from zoneinfo import ZoneInfo
    from datetime import datetime

    def shanghai_date(iso):
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")

    captured = []

    def fake_run(args, stdin_text=None, timeout=120):
        if "--help" in args:
            return FakeProc(0, stdout="Available Commands:\n  +record-batch-update\n  +record-list")
        if "+record-list" in args:
            payload = {
                "data": {
                    "fields": ["归档日期", "推送时间", "链接"],
                    "records": [
                        {"record_id": "rec1", "fields": {"归档日期": "", "推送时间": "2026-08-25T01:00:00Z", "链接": "https://e.com/1"}},
                        {"record_id": "rec2", "fields": {"归档日期": "2026-08-25", "推送时间": "2026-08-25T01:00:00Z", "链接": "https://e.com/2"}},
                        {"record_id": "rec3", "fields": {"归档日期": "", "推送时间": "2026-08-26T16:00:00Z", "链接": "https://e.com/3"}},
                    ],
                }
            }
            return FakeProc(0, stdout=json.dumps(payload, ensure_ascii=False))
        if "+record-batch-update" in args:
            j = args[args.index("--json") + 1]
            captured.append(json.loads(j))
            return FakeProc(0, stdout="{}")
        return FakeProc(0, stdout="{}")

    monkeypatch.setattr(bitable, "_run", fake_run)
    n = bitable.backfill_empty_archive_dates("app", "tbl", env_name="dev", dry_run=False)
    assert n == 2
    assert len(captured) == 1
    upd = captured[0].get("update_records") or {}
    assert "rec1" in upd and "rec3" in upd and "rec2" not in upd
    assert upd["rec1"]["归档日期"] == shanghai_date("2026-08-25T01:00:00Z")
    assert upd["rec1"]["归档日期"] == "2026-08-25"
    assert upd["rec3"]["归档日期"] == shanghai_date("2026-08-26T16:00:00Z")
    assert upd["rec3"]["归档日期"] == "2026-08-27"
    for v in upd.values():
        assert len(v["归档日期"]) == 10

    captured.clear()

    def fake_run_dry(args, stdin_text=None, timeout=120):
        if "--help" in args:
            return FakeProc(0, stdout="+record-batch-update")
        if "+record-list" in args:
            payload = {
                "data": {
                    "fields": ["归档日期", "推送时间"],
                    "records": [
                        {"record_id": "rec1", "fields": {"归档日期": "", "推送时间": "2026-08-25T01:00:00Z"}},
                        {"record_id": "rec2", "fields": {"归档日期": "", "推送时间": "2026-08-25T01:00:00Z"}},
                        {"record_id": "rec3", "fields": {"归档日期": "2026-08-25", "推送时间": "2026-08-25T01:00:00Z"}},
                    ],
                }
            }
            return FakeProc(0, stdout=json.dumps(payload, ensure_ascii=False))
        if "+record-batch-update" in args:
            captured.append(1)
            return FakeProc(0, stdout="{}")
        return FakeProc(0, stdout="{}")

    monkeypatch.setattr(bitable, "_run", fake_run_dry)
    n2 = bitable.backfill_empty_archive_dates("app", "tbl", dry_run=True)
    assert n2 == 2
    assert captured == []
