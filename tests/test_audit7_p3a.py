"""第七轮审计 P3-A（#293/#294/#297–#306）——失败优先验收。

全离线：lark-cli/httpx 一律 mock。
"""

from __future__ import annotations

import json

import pytest

from feedkicker import (
    bitable_backfill,
    bitable_lark,
    bitable_purge,
    bitable_records,
    bitable_reseed,
    bitable_views,
    extract_flow,
    extract_llm,
    feishu,
    push,
    store,
    topic_records,
)
from feedkicker.config import Config, Feed, HttpConf, SiteConf
from feedkicker.extract_parse import parse_topics
from feedkicker.extract_write import existing_index, plan_writes, write_topics


class FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeResp:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _topic(name: str, link: str) -> dict:
    return {
        "话题名称": name,
        "可使用工具": "工具",
        "相关AI原理": "原理",
        "资讯链接": [link],
        "出处来源": ["量子位"],
    }


def _full_topic(name: str) -> dict:
    return {
        "话题名称": name,
        "可使用工具": "工具X",
        "相关AI原理": "原理Y",
        "资讯链接": ["https://a/1"],
        "出处来源": ["量子位"],
    }


# ── #293 跳过分支也须记录 seen_links ──


def test_plan_writes_records_links_from_skipped_rows() -> None:
    topics = [_topic("话题A", "https://e.com/shared"), _topic("话题B", "https://e.com/shared")]

    picked, skipped = plan_writes(topics, "MMax", "2026-09-14", {"话题a"}, {"https://e.com/old"})

    assert picked == []
    assert [r["话题名称"] for r in skipped] == ["话题A", "话题B"]


# ── #294 空白话题名计 dropped，不再算 empty_batch ──


def test_parse_topics_blank_name_counted_dropped() -> None:
    raw = json.dumps({"topics": [_full_topic("   ")]}, ensure_ascii=False)

    topics, dropped = parse_topics(raw)

    assert topics == [] and dropped == 1


def test_parse_topics_blank_name_plus_good_keeps_good() -> None:
    raw = json.dumps({"topics": [_full_topic("　"), _full_topic("好话题")]}, ensure_ascii=False)

    topics, dropped = parse_topics(raw)

    assert [t["话题名称"] for t in topics] == ["好话题"]
    assert dropped == 1


def test_refine_batches_all_blank_names_count_failed(monkeypatch) -> None:
    from feedkicker.config_models import ExtractConf

    raw = json.dumps({"topics": [_full_topic("  ")]}, ensure_ascii=False)
    monkeypatch.setattr(extract_llm, "call_llm", lambda ex, prompt: raw)

    collected, calls, failed, empty = extract_llm.refine_batches(
        ExtractConf(), "模板", [[{"title": "t", "url": "https://a/1"}]], 0
    )

    assert collected == [] and failed == 1 and empty == 0 and calls == 1


# ── #297 空 fields + 有数据行须 raise，空 data 为合法空表 ──


def test_existing_index_empty_fields_with_data_rows_raises(monkeypatch) -> None:
    body = json.dumps({"data": {"fields": [], "data": [["x"]]}}, ensure_ascii=False)
    monkeypatch.setattr(bitable_lark, "_run", lambda *a, **k: FakeProc(0, body))

    with pytest.raises(RuntimeError, match="话题名称|无法识别"):
        existing_index("app", "tbl")


def test_existing_index_empty_fields_empty_data_is_valid_empty(monkeypatch) -> None:
    body = json.dumps({"data": {"fields": [], "data": []}}, ensure_ascii=False)
    monkeypatch.setattr(bitable_lark, "_run", lambda *a, **k: FakeProc(0, body))

    assert existing_index("app", "tbl") == (set(), set())


# ── #298 write_topics 返回失败条数，summary 可见 ──


def test_write_topics_returns_failed_count(monkeypatch) -> None:
    calls = {"n": 0}

    def fake_run(args, stdin_text=None, timeout=120):
        if "+record-list" in args:
            return FakeProc(0, '{"data": {"records": []}}')
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeProc(0, "{}")
        return FakeProc(0, '{"ok": false, "error": {"message": "boom"}}')

    monkeypatch.setattr(bitable_lark, "_run", fake_run)
    topics = [{"话题名称": f"T{i}", "资讯链接": [f"https://e/{i}"]} for i in range(205)]

    assert write_topics("app", "tbl", topics, "MMax", "2026-09-14") == (200, 0, 5)


def test_extract_apply_summary_reports_failed_writes(tmp_path, monkeypatch, capsys) -> None:
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "salon:\n  app_token: appT\n  table_id: tblT\n"
        "extract:\n  enabled: true\n  provider: minimax\n  batch_size: 2\n"
        "  providers:\n    minimax:\n      api_key: sk-t\n",
        encoding="utf-8",
    )

    def fake_run(args, stdin_text=None, timeout=120):
        if "+record-list" in args:
            return FakeProc(0, '{"data": {"records": []}}')
        return FakeProc(0, '{"ok": false, "error": {"message": "denied"}}')

    monkeypatch.setattr(bitable_lark, "_run", fake_run)
    monkeypatch.setattr(
        extract_flow.extract_source,
        "select_source",
        lambda conn, since_days, limit=None: [
            {"feed_id": "F", "entry_key": "k", "title": "t", "url": "https://e/1",
             "description": "", "published_at": None, "first_seen": "2026-09-14T00:00:00Z"}
        ],
    )
    topic = json.dumps({"topics": [_full_topic("话题A")]}, ensure_ascii=False)
    monkeypatch.setattr(extract_flow.extract_llm, "call_llm", lambda ex, prompt: topic)
    monkeypatch.delenv("MiniMax_Key", raising=False)
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)

    rc = extract_flow.main(["--apply", "--config", str(cfg), "--db", str(tmp_path / "t.sqlite3")])

    out = capsys.readouterr().out
    stats = json.loads([ln for ln in out.splitlines() if ln.startswith("{")][-1])
    assert rc == 1
    assert stats["written"] == 0 and stats["failed_writes"] == 1


# ── #299 bootstrap_days 上界 + promise_skip_old 失败不盖 first_run ──


def test_bootstrap_days_over_bound_rc2(tmp_path) -> None:
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("bootstrap_days: 10000000000\nfeeds: []\n", encoding="utf-8")

    assert push.main(["--config", str(cfg), "--db", str(tmp_path / "t.sqlite3"), "--env", "test"]) == 2


def test_promise_skip_old_failure_keeps_first_run(monkeypatch) -> None:
    conn = store.connect(":memory:")
    cfg = Config(
        feishu_webhook="",
        bootstrap_days=3,
        http=HttpConf(timeout_seconds=5),
        feeds=[Feed(name="F", url="https://e.com/rss")],
        site=SiteConf(),
    )
    entries = [
        {"entry_key": "k", "title": "old", "url": "https://e.com/o",
         "description": "", "published_at": "2020-01-01T00:00:00Z"}
    ]
    monkeypatch.setattr(push, "fetch_feed", lambda u, h: entries)
    monkeypatch.setattr(
        push.store, "promise_skip_old",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db boom")),
    )
    monkeypatch.setattr(feishu, "send", lambda *a, **k: False)

    push.run(cfg, conn)

    assert store.is_first_run(conn, "F")
    row = conn.execute("SELECT pushed_at FROM articles WHERE url='https://e.com/o'").fetchone()
    assert row is None or row[0] is None
    conn.close()


# ── #300 仅显式 0 业务码算成功 ──


@pytest.mark.parametrize(
    ("payload", "ok"),
    [
        ({"StatusCode": 0}, True),
        ({"code": 0}, True),
        ({}, False),
        ({"StatusCode": None, "code": 0}, False),
    ],
)
def test_send_requires_explicit_zero_code(monkeypatch, payload, ok) -> None:
    monkeypatch.setattr(feishu.httpx, "post", lambda *a, **k: FakeResp(payload))

    assert feishu.send({"msg_type": "text"}, "https://hook.test/x", 5, "ua") is ok


# ── #301 topic_records fields 容器类型漂移须 raise ──


def test_topic_extract_fields_dict_raises() -> None:
    with pytest.raises(RuntimeError, match="fields"):
        topic_records._extract_records({"fields": {"a": 1}, "data": [[1, 2]]})


def test_topic_extract_fields_list_rows_still_works() -> None:
    recs = topic_records._extract_records({"fields": ["话题名称"], "data": [["T"]]})

    assert recs == [{"record_id": "", "fields": {"话题名称": "T"}}]


def test_sos_send_text_failure_keeps_streak(monkeypatch) -> None:
    conn = store.connect(":memory:")
    cfg = Config(
        feishu_webhook="hook-x",
        http=HttpConf(timeout_seconds=5),
        feeds=[Feed(name="F", url="https://e.com/rss")],
        site=SiteConf(),
    )
    entries = [{"entry_key": "k", "title": "t", "url": "https://e.com/1", "description": "", "published_at": None}]
    monkeypatch.setattr(push, "fetch_feed", lambda u, h: entries)
    monkeypatch.setattr(feishu, "send", lambda *a, **k: False)
    monkeypatch.setattr(feishu, "send_text", lambda *a, **k: False)
    store.set_meta(conn, push.PUSH_FAIL_STREAK_KEY, "2")

    assert push.run(cfg, conn) == 1
    assert store.get_meta(conn, push.PUSH_FAIL_STREAK_KEY) == "3"
    conn.close()


def test_sos_send_text_success_clears_streak(monkeypatch) -> None:
    conn = store.connect(":memory:")
    cfg = Config(
        feishu_webhook="hook-x",
        http=HttpConf(timeout_seconds=5),
        feeds=[Feed(name="F", url="https://e.com/rss")],
        site=SiteConf(),
    )
    entries = [{"entry_key": "k", "title": "t", "url": "https://e.com/1", "description": "", "published_at": None}]
    monkeypatch.setattr(push, "fetch_feed", lambda u, h: entries)
    monkeypatch.setattr(feishu, "send", lambda *a, **k: False)
    monkeypatch.setattr(feishu, "send_text", lambda *a, **k: True)
    store.set_meta(conn, push.PUSH_FAIL_STREAK_KEY, "2")

    assert push.run(cfg, conn) == 1
    assert store.get_meta(conn, push.PUSH_FAIL_STREAK_KEY) == "0"
    conn.close()


# ── #303 页指纹仅基于 record id ──


def test_page_fingerprint_hashes_idless_data_rows() -> None:
    page = {"fields": ["链接"], "data": [{"链接": "https://e.com/x"} for _ in range(3)]}

    fp = bitable_lark._page_fingerprint(page)
    assert fp
    assert bitable_lark._page_guard("", page) == fp


def test_existing_links_idless_identical_pages_trip_on_second(monkeypatch) -> None:
    rows = [{"链接": "https://e.com/same"} for _ in range(200)]
    calls = {"n": 0}

    def fake_run(args, stdin_text=None, timeout=120):
        calls["n"] += 1
        return FakeProc(0, json.dumps({"data": {"fields": ["链接"], "data": rows}}))

    monkeypatch.setattr(bitable_lark, "_run", fake_run)

    with pytest.raises(RuntimeError, match="分页|offset"):
        bitable_records.existing_links("app", "tbl")
    assert calls["n"] == 2


def test_existing_links_idless_records_pagination_bounded(monkeypatch) -> None:
    recs = [{"fields": {"链接": "https://e.com/x"}} for _ in range(200)]
    calls = {"n": 0}
    monkeypatch.setattr(bitable_lark, "_MAX_PAGES", 3)

    def fake_run(args, stdin_text=None, timeout=120):
        calls["n"] += 1
        return FakeProc(0, json.dumps({"data": {"records": recs}}))

    monkeypatch.setattr(bitable_lark, "_run", fake_run)

    with pytest.raises(RuntimeError, match="页数|offset|分页"):
        bitable_records.existing_links("app", "tbl")
    assert 2 < calls["n"] <= 5


# ── #304 records 子项非 dict 须 raise（覆盖 records/purge/backfill/views） ──


def test_existing_links_records_non_dict_raises(monkeypatch) -> None:
    body = json.dumps({"data": {"records": ["x"]}})
    monkeypatch.setattr(bitable_lark, "_run", lambda *a, **k: FakeProc(0, body))

    with pytest.raises(RuntimeError):
        bitable_records.existing_links("app", "tbl")


def test_list_records_records_non_dict_raises(monkeypatch) -> None:
    body = json.dumps({"data": {"records": ["x"]}})
    monkeypatch.setattr(bitable_lark, "_run", lambda *a, **k: FakeProc(0, body))

    with pytest.raises(RuntimeError):
        bitable_purge._list_records("app", "tbl")


def test_backfill_records_non_dict_raises(monkeypatch) -> None:
    body = json.dumps({"data": {"records": ["x"]}})
    monkeypatch.setattr(bitable_lark, "_run", lambda *a, **k: FakeProc(0, body))

    with pytest.raises(RuntimeError):
        bitable_backfill.backfill_empty_archive_dates("app", "tbl")


def test_views_field_list_non_dict_raises(monkeypatch) -> None:
    body = json.dumps({"data": {"fields": ["x"]}})
    monkeypatch.setattr(bitable_lark, "_run", lambda *a, **k: FakeProc(0, body))

    with pytest.raises(RuntimeError):
        bitable_views.ensure_archive_date_field("app", "tbl")


# ── #305 reseed markdown 清空循环有页数上限 ──


def test_reseed_markdown_loop_bounded(monkeypatch) -> None:
    header = "| _record_id | 标题 |\n| --- | --- |"
    page = header + "\n" + "\n".join(f"| recR{i:06d} | t |" for i in range(200))
    calls = {"n": 0}
    monkeypatch.setattr(bitable_lark, "_MAX_PAGES", 3)

    def fake_run(args, stdin_text=None, timeout=120):
        calls["n"] += 1
        if "+record-delete" in args:
            return FakeProc(0, "{}")
        return FakeProc(0, page)

    monkeypatch.setattr(bitable_lark, "_run", fake_run)

    with pytest.raises(RuntimeError, match="页数|分页|offset"):
        bitable_reseed.purge_all_records("app", "tbl", env_name=None)
    assert calls["n"] <= 8
