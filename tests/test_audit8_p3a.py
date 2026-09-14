"""第八轮审计 P3-A（#330–#348）——失败优先验收。全离线：lark-cli/httpx 一律 mock。"""

from __future__ import annotations

import json
import logging

import pytest

from feedkicker import (
    bitable,
    bitable_backfill,
    bitable_lark,
    bitable_records,
    extract_flow,
    extract_llm,
    extract_write,
    minimax,
    push,
    salon_md,
    salon_notify,
    store,
)
from feedkicker import feishu_card as feishu
from feedkicker import topic as topic_mod
from feedkicker.config import PROJECT_ROOT
from feedkicker.config import load_config as load_cfg
from feedkicker.config_models import Config, ExtractConf, Feed, HttpConf, SiteConf
from feedkicker.extract_parse import build_batch_prompt, parse_topics, strip_reasoning
from feedkicker.extract_write import link_keys, plan_writes
from feedkicker.fetch import dedup_key


class FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeResp:
    def __init__(self, status_code: int = 200, payload: object | None = None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _item(feed: str, key: str, title: str, url: str, desc: str = "") -> dict:
    return {
        "feed_id": feed,
        "entry_key": key,
        "title": title,
        "url": url,
        "description": desc,
        "published_at": None,
        "first_seen": None,
    }


def _outline() -> dict:
    return {
        "title": "大纲",
        "slides": [{"heading": "h", "bullets": ["a"], "speaker_note": "n"}],
    }


# ── #330 extract_llm._content_of reasoning_content 回退 ──


@pytest.mark.parametrize("content", [None, ""])
def test_content_of_falls_back_to_reasoning(content) -> None:
    data = {"choices": [{"message": {"content": content, "reasoning_content": "X"}}]}
    assert extract_llm._content_of(data) == "X"


def test_content_of_prefers_real_content() -> None:
    data = {"choices": [{"message": {"content": "real", "reasoning_content": "X"}}]}
    assert extract_llm._content_of(data) == "real"


def test_post_chat_uses_reasoning_fallback(monkeypatch) -> None:
    payload = {"choices": [{"message": {"content": None, "reasoning_content": '{"topics": []}'}}]}
    monkeypatch.setattr(extract_llm.httpx, "post", lambda *a, **k: FakeResp(200, payload))
    conf = extract_llm.ProviderConf(base_url="https://api.example.com/v1", model="m", api_key="sk")
    assert extract_llm._post_chat(conf, "p") == '{"topics": []}'


# ── #331 嵌套 think 剥离 + JSON 字符串内标签保留 ──


def test_strip_reasoning_nested_block() -> None:
    raw = '<think>a{1}<think>b{2}</think>c{3}</think>{"topics": []}'
    assert strip_reasoning(raw) == '{"topics": []}'


def test_strip_reasoning_keeps_tags_inside_string_literal() -> None:
    raw = '"比较 <thinking>X</thinking> 模型"'
    assert strip_reasoning(raw) == raw


def test_parse_topics_nested_think_parses() -> None:
    raw = '<think>a{1}<think>b{2}</think>c{3}</think>{"topics": []}'
    assert parse_topics(raw) == ([], 0)


def test_parse_topics_keeps_tag_inside_json_string() -> None:
    raw = json.dumps({"topics": [_topic(name="比较 <thinking>X</thinking> 模型")]}, ensure_ascii=False)
    got, dropped = parse_topics(raw)
    assert dropped == 0
    assert got[0]["话题名称"] == "比较 <thinking>X</thinking> 模型"


def _topic(name: str = "X", tool: str = "T", principle: str = "P") -> dict:
    return {
        "话题名称": name,
        "可使用工具": tool,
        "相关AI原理": principle,
        "资讯链接": ["https://a/1"],
        "出处来源": ["S"],
    }


# ── #332/#333 dedup_key：尾斜杠 / HTML 实体归一 + tracking 白名单（移出 source/from/ref） ──


def test_dedup_key_trailing_slash_equivalent() -> None:
    assert dedup_key("https://e.com/x") == dedup_key("https://e.com/x/")
    assert dedup_key("https://e.com/x") == "https://e.com/x"


def test_dedup_key_html_entity_amp_equivalent() -> None:
    assert dedup_key("https://e.com/p?a=1&amp;b=2") == dedup_key("https://e.com/p?a=1&b=2")


def test_dedup_key_source_not_stripped() -> None:
    assert dedup_key("https://e.com/p?source=alpha") != dedup_key("https://e.com/p?source=beta")


def test_dedup_key_still_strips_utm_and_fbclid() -> None:
    assert dedup_key("https://e.com/p?utm_source=rss") == dedup_key("https://e.com/p")
    assert dedup_key("https://e.com/p?fbclid=abc") == dedup_key("https://e.com/p")


def test_link_keys_trailing_slash_and_entity_equivalent() -> None:
    assert link_keys("https://e.com/path/") == link_keys("https://e.com/path")
    assert link_keys("https://e.com/p?a=1&amp;b=2") == link_keys("https://e.com/p?a=1&b=2")


def test_plan_writes_source_variants_not_merged() -> None:
    existing = link_keys("https://e.com/p?source=alpha")
    topics = [{"话题名称": "B", "资讯链接": ["https://e.com/p?source=beta"]}]
    picked, skipped = plan_writes(topics, "DS", "2026-09-15", set(), existing)
    assert [p["话题名称"] for p in picked] == ["B"]
    assert skipped == []


# ── #334 refine_batches：max_calls 达限把当前批计入 failed ──


def test_refine_batches_max_calls_counts_failed(monkeypatch) -> None:
    def boom(cfg, prompt):
        raise RuntimeError("boom")

    monkeypatch.setattr(extract_llm, "call_llm", boom)
    collected, calls, failed, empty = extract_llm.refine_batches(
        ExtractConf(), "t", [[{"title": "a", "url": "u", "description": ""}]], max_calls=1
    )

    assert (collected, calls, failed, empty) == ([], 1, 1, 0)


# ── #335 prompt 体积上界 + MAX_BATCH_SIZE ──


def test_build_batch_prompt_truncates_long_title_and_url() -> None:
    out = build_batch_prompt("t", [{"title": "字" * 300, "url": "u" * 600, "description": ""}])

    assert "字" * 200 in out and "字" * 201 not in out
    assert "u" * 500 in out and "u" * 501 not in out


def test_extract_run_batch_size_over_bound_rc2() -> None:
    conn = store.connect(":memory:")
    rc = extract_flow.run(Config(app_env="test"), conn, apply=False, batch_size=999999999)
    conn.close()
    assert rc == 2


def test_load_config_batch_size_over_bound_raises(tmp_path) -> None:
    path = tmp_path / "cfg.yaml"
    path.write_text("extract:\n  batch_size: 999\n", encoding="utf-8")
    with pytest.raises(ValueError, match="batch_size"):
        load_cfg(config_path=path, app_env="test")


# ── #336 文档补 failed_writes ──


def test_docs_mention_failed_writes() -> None:
    for name in ("CLI.md", "DESIGN.md"):
        assert "failed_writes" in (PROJECT_ROOT / "docs" / name).read_text(encoding="utf-8")


# ── #337 escape_inline 尖括号转义 ──


def test_escape_inline_escapes_angle_brackets() -> None:
    out = feishu.escape_inline("hello <at id=all></at> world")
    assert "<at" not in out
    assert "&lt;at id=all&gt;&lt;/at&gt;" in out


# ── #338 detail_url 为空时强制 top_n=0 ──


def test_push_no_detail_url_keeps_all_items(monkeypatch) -> None:
    conn = store.connect(":memory:")
    cfg = Config(
        feishu_webhook="",
        http=HttpConf(timeout_seconds=5),
        feeds=[Feed(name="F", url="https://e.com/rss")],
        site=SiteConf(top_n=3),
    )
    cfg.bitable.enabled = True
    entries = [_item("F", f"k{i}", f"t{i}", f"https://e.com/{i}") for i in range(6)]
    monkeypatch.setattr(push, "fetch_feed", lambda u, h: entries)
    monkeypatch.setattr(push.bitable_records, "sync_env", lambda *a, **k: 0)
    sent: list[dict] = []
    monkeypatch.setattr(push.feishu, "send", lambda payload, *a, **k: sent.append(payload) or True)

    assert push.run(cfg, conn) == 0
    content = sent[0]["card"]["elements"][0]["text"]["content"]
    assert content.count("https://e.com/") == 6
    conn.close()


# ── #339 salon SOS send_text 失败保留连败 ──


def _salon_cfg() -> Config:
    return Config(
        feishu_webhook="hook-x",
        http=HttpConf(timeout_seconds=5),
        site=SiteConf(),
        app_env="test",
    )


def test_salon_sos_failure_keeps_streak(monkeypatch) -> None:
    conn = store.connect(":memory:")
    monkeypatch.setattr(salon_notify.feishu, "send", lambda *a, **k: False)
    monkeypatch.setattr(salon_notify.feishu, "send_text", lambda *a, **k: False)
    store.set_meta(conn, salon_notify.SALON_FAIL_STREAK_KEY, "2")

    assert salon_notify.send_wiki_card(_salon_cfg(), conn, ["https://x/wiki/1"]) is False
    assert store.get_meta(conn, salon_notify.SALON_FAIL_STREAK_KEY) == "3"
    conn.close()


def test_salon_sos_success_clears_streak(monkeypatch) -> None:
    conn = store.connect(":memory:")
    monkeypatch.setattr(salon_notify.feishu, "send", lambda *a, **k: False)
    monkeypatch.setattr(salon_notify.feishu, "send_text", lambda *a, **k: True)
    store.set_meta(conn, salon_notify.SALON_FAIL_STREAK_KEY, "2")

    salon_notify.send_wiki_card(_salon_cfg(), conn, ["https://x/wiki/1"])
    assert store.get_meta(conn, salon_notify.SALON_FAIL_STREAK_KEY) == "0"
    conn.close()


# ── #340 topic_records record_id=null 不覆盖别名 ──


def test_extract_records_null_record_id_keeps_alias() -> None:
    data = {
        "fields": ["话题名称"],
        "data": [{"record_id": None, "recordId": "rec_REAL", "fields": {"话题名称": "T"}}],
    }
    recs = topic_mod._extract_records(data)
    assert recs[0]["record_id"] == "rec_REAL"


# ── #341 push SOS 文案按实际同步结果 ──


def test_push_sos_text_reflects_sync_failure(monkeypatch, caplog) -> None:
    conn = store.connect(":memory:")
    cfg = Config(
        feishu_webhook="hook-x",
        http=HttpConf(timeout_seconds=5),
        feeds=[Feed(name="F", url="https://e.com/rss")],
        site=SiteConf(),
    )
    cfg.bitable.enabled = True
    cfg.bitable.app_token = "app"
    cfg.bitable.table_id = "tbl"
    cfg.bitable.url = "https://base.example/tbl"
    entries = [_item("F", "k", "t", "https://e.com/1")]
    monkeypatch.setattr(push, "fetch_feed", lambda u, h: entries)
    monkeypatch.setattr(
        push.bitable_records, "sync_env", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    monkeypatch.setattr(push.feishu, "send", lambda *a, **k: False)
    sos: list[str] = []
    monkeypatch.setattr(push.feishu, "send_text", lambda msg, *a, **k: sos.append(msg) or True)
    store.set_meta(conn, push.PUSH_FAIL_STREAK_KEY, "2")

    with caplog.at_level(logging.WARNING):
        assert push.run(cfg, conn) == 1
    assert sos and "已入档" not in sos[0]
    conn.close()


# ── #342 日志轮转文档（newsyslog + webhook 轮换提示） ──


def test_ops_documents_log_rotation_and_webhook_rotation() -> None:
    text = (PROJECT_ROOT / "docs" / "OPS.md").read_text(encoding="utf-8")
    assert "newsyslog" in text
    assert "logs/*.log" in text
    assert "轮换" in text and "webhook" in text


# ── #343 卡片链接 ( / 空格 转义 ──


def test_build_card_link_paren_and_space_escaped() -> None:
    items = [_item("F", "k", "t", "https://e.com/a (b)c")]
    content = feishu.build_card(items, 0, ["F"])["card"]["elements"][0]["text"]["content"]
    assert "[t](https://e.com/a%20%28b%29c)" in content


# ── #344 backfill 中途失败显式收口 ──


def test_backfill_run_none_mid_page_raises(monkeypatch) -> None:
    monkeypatch.setattr(bitable_lark, "_has_batch_verb", lambda: "+record-batch-update")
    monkeypatch.setattr(bitable_lark, "_run", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="lark-cli|backfill|无返回"):
        bitable_backfill.backfill_empty_archive_dates("app", "tbl")


def test_backfill_business_failure_warns(monkeypatch, caplog) -> None:
    page = {
        "records": [
            {"record_id": f"r{i}", "fields": {"归档日期": "", "推送时间": "2026-01-01 10:00"}}
            for i in range(200)
        ]
    }
    calls = {"n": 0}

    def fake_run(args, stdin_text=None, timeout=120):
        if "+record-list" not in args:
            return FakeProc(0, "+record-batch-update")
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeProc(0, json.dumps({"data": page}))
        return FakeProc(0, json.dumps({"ok": False, "error": {"message": "boom"}}))

    monkeypatch.setattr(bitable_lark, "_has_batch_verb", lambda: "+record-batch-update")
    monkeypatch.setattr(bitable_lark, "_run", fake_run)

    with caplog.at_level(logging.WARNING):
        n = bitable_backfill.backfill_empty_archive_dates("app", "tbl", dry_run=True)

    assert n == 200
    assert any("backfill" in r.getMessage() for r in caplog.records)


def test_bitable_main_backfill_none_rc2(monkeypatch, tmp_path) -> None:
    cfg = Config(app_env="test")
    cfg.bitable.enabled = True
    cfg.bitable.app_token = "appReal"
    cfg.bitable.table_id = "tblReal"
    cfg.db_path = tmp_path / "b.sqlite3"
    monkeypatch.setattr("feedkicker.config.load_config", lambda *a, **k: cfg)
    from feedkicker import bitable_schema

    monkeypatch.setattr(
        bitable_schema, "ensure_initialized", lambda bt, env: {"app_token": "a", "table_id": "t", "url": "u"}
    )
    monkeypatch.setattr(bitable_lark, "lark_bin", lambda: "/usr/bin/lark-cli")
    monkeypatch.setattr(bitable_lark, "_has_batch_verb", lambda: "+record-batch-update")
    monkeypatch.setattr(bitable_lark, "_run", lambda *a, **k: None)

    assert bitable.main(["--backfill", "--env", "test"]) == 2


# ── #345 records[].fields 非 dict 收口 ──


def test_existing_links_non_dict_fields_no_crash(monkeypatch) -> None:
    payload = {"records": [{"record_id": "r1", "fields": ["链接"]}, {"record_id": "r2", "fields": "oops"}]}
    monkeypatch.setattr(bitable_lark, "_run", lambda *a, **k: FakeProc(0, json.dumps({"data": payload})))
    assert bitable_records.existing_links("app", "tbl") == set()


def test_existing_index_non_dict_fields_no_crash(monkeypatch) -> None:
    payload = {"records": [{"record_id": "r1", "fields": ["资讯链接"]}]}
    monkeypatch.setattr(bitable_lark, "_run", lambda *a, **k: FakeProc(0, json.dumps({"data": payload})))
    assert extract_write.existing_index("app", "tbl") == (set(), set())


# ── #346 minimax 解析加固（think / reasoning_content / arguments） ──


def _minimax_conf() -> ExtractConf:
    return ExtractConf(provider="minimax", providers={"minimax": extract_llm.ProviderConf(api_key="sk")})


@pytest.mark.parametrize(
    "message",
    [
        {"content": "<think>r{1}</think>" + json.dumps(_outline()), "tool_calls": []},
        {"content": "", "reasoning_content": json.dumps(_outline()), "tool_calls": []},
        {
            "tool_calls": [
                {
                    "function": {
                        "name": "generate_ppt_outline",
                        "arguments": "<think>r</think>" + json.dumps(_outline()),
                    }
                }
            ]
        },
    ],
)
def test_minimax_parse_think_and_reasoning_forms(monkeypatch, message) -> None:
    monkeypatch.setattr(minimax.httpx, "post", lambda *a, **k: FakeResp(200, {"choices": [{"message": message}]}))
    assert minimax.gen_outline("topic", kind="tool", api_key="sk") == _outline()


# ── #347 _slides_of 校验 bullets ──


@pytest.mark.parametrize(
    "outline",
    [
        {"slides": [{"heading": "h"}]},
        {"slides": [{"heading": "h", "bullets": "str"}]},
        {"slides": [{"heading": "h", "bullets": [{"a": 1}]}]},
    ],
)
def test_slides_of_requires_bullets(outline) -> None:
    with pytest.raises(ValueError):
        salon_md._slides_of(outline)


def test_slides_of_valid_with_bullets() -> None:
    assert salon_md._slides_of({"slides": [{"heading": "h", "bullets": ["a"]}]}) == [
        {"heading": "h", "bullets": ["a"]}
    ]


# ── #348 topic 页数守卫 + limit 上界 + 交替空页熔断 ──


def test_topic_limit_alternating_empty_trips_fast(monkeypatch) -> None:
    calls = {"n": 0}

    def fake_run(args, stdin_text=None, timeout=120):
        calls["n"] += 1
        assert calls["n"] < 20, "交替空页必须在有限次数内熔断"
        if calls["n"] % 2 == 1:
            recs = [{"record_id": f"rec{calls['n']}", "fields": {"讨论状态": ["已选题"]}}]
            return FakeProc(0, json.dumps({"data": {"records": recs, "has_more": True}}))
        return FakeProc(0, json.dumps({"data": {"records": [], "has_more": True}}))

    monkeypatch.setattr(topic_mod.bitable_lark, "_run", fake_run)
    with pytest.raises(RuntimeError):
        topic_mod.fetch_selected_topics("app", "tbl", limit=1)
    assert calls["n"] < 20


def test_topic_limit_clamped_to_200(monkeypatch) -> None:
    captured: list[str] = []

    def fake_run(args, stdin_text=None, timeout=120):
        captured.append(args[args.index("--limit") + 1])
        return FakeProc(0, json.dumps({"data": {"records": [], "has_more": False}}))

    monkeypatch.setattr(topic_mod.bitable_lark, "_run", fake_run)
    assert topic_mod.fetch_selected_topics("app", "tbl", limit=99999) == []
    assert captured[0] == "200"
