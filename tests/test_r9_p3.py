"""R9 P3 批次 B：reasoning/解析/去重 + bitable/topic/salon 边界（全离线，#380）。"""

from __future__ import annotations

import json
import time

import pytest

from feedkicker import (
    bitable_backfill,
    bitable_lark,
    extract_llm,
    feishu_card,
    minimax_parse,
    reasoning,
    topic,
    topic_records,
)
from feedkicker.extract_parse import _load_json_obj, md_link_tokens, parse_topics
from feedkicker.fetch import dedup_key, trim_url


class FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_r9_17_all_fences_and_braced_fallback() -> None:
    raw = '```text\n推理 {不是 JSON}\n```\n```json\n{"topics": []}\n```'
    assert _load_json_obj(raw) == {"topics": []}
    assert parse_topics('前言{注} 后 {"topics": []}')[0] == []


def test_r9_18_root_trailing_slash_equivalent() -> None:
    assert dedup_key("https://a.com/") == dedup_key("https://a.com")


def test_r9_19_strips_wrapping_punctuation_keeps_balanced_parens() -> None:
    assert trim_url("https://a.com/1，详见") == "https://a.com/1"
    assert dedup_key("(https://a.com/1)") == "https://a.com/1"
    assert dedup_key("<https://a.com/1>") == "https://a.com/1"
    assert md_link_tokens("见 (https://a.com/1)，详见") == ["https://a.com/1"]
    assert trim_url("https://e.com/a_(b_(c))") == "https://e.com/a_(b_(c))"


def test_r9_20_content_blocks_extract_and_minimax() -> None:
    data = {"choices": [{"message": {"content": [{"text": "he"}, {"content": "llo"}]}}]}
    assert extract_llm._content_of(data) == "hello"
    outline = {"slides": [{"bullets": ["x"]}]}
    mdata = {"choices": [{"message": {"content": [{"text": json.dumps(outline)}]}}]}
    assert minimax_parse.parse_outline_from_response(mdata) == outline


def test_r9_21_tag_at_large_text_fast() -> None:
    text = ("x" * 190_000) + "<think>r</think>{\"topics\": []}"
    start = time.monotonic()
    assert reasoning.strip_reasoning(text).endswith('{"topics": []}')
    assert time.monotonic() - start < 1.0


def test_r9_22_record_id_camel_and_alias_stripped() -> None:
    recs = topic_records._extract_records(
        {"fields": ["话题名称"], "data": [{"recordId": "recCamel", "话题名称": "X"}]}
    )
    assert recs[0]["record_id"] == "recCamel" and "recordId" not in recs[0]["fields"]


def test_r9_26_records_non_list_raises() -> None:
    with pytest.raises(RuntimeError):
        topic_records._extract_records({"records": {}})
    assert topic_records._extract_records({"records": []}) == []


def test_r9_31_dict_row_fields_non_dict_collapses() -> None:
    recs = topic_records._extract_records({"fields": ["话题名称"], "data": [{"fields": ["bad"]}]})
    assert recs[0]["fields"] == {}


def test_r9_30_slides_whitespace_bullets_rejected() -> None:
    from feedkicker import salon_md

    with pytest.raises(ValueError):
        salon_md._slides_of({"slides": [{"bullets": ["   ", ""]}]})


def test_r9_33_compact_date_not_epoch() -> None:
    assert bitable_backfill._shanghai_date("20260101") == "2026-01-01"
    assert bitable_backfill._shanghai_date("1735689600000") == "2025-01-01"


def test_r9_24_hash_route_fragment_kept_section_dropped() -> None:
    assert dedup_key("https://a.com/#/post/1") != dedup_key("https://a.com/#/post/2")
    assert dedup_key("https://a.com/page#section") == dedup_key("https://a.com/page")


def test_r9_23_backfill_unrecognizable_page_raises(monkeypatch) -> None:
    monkeypatch.setattr(bitable_lark, "_run", lambda *a, **k: FakeProc(0, '{"ok": true}'))
    monkeypatch.setattr(bitable_lark, "_has_batch_verb", lambda: "+record-batch-update")

    with pytest.raises(RuntimeError, match="无法识别"):
        bitable_backfill.backfill_empty_archive_dates("app", "tbl")


def test_r9_29_backfill_all_write_fail_raises(monkeypatch) -> None:
    calls = {"n": 0}

    def fake_run(args, stdin_text=None, timeout=120):
        if args[:2] == ["base", "--help"]:
            return FakeProc(0, "+record-batch-update")
        if "+record-list" in args:
            page = {"fields": ["归档日期", "推送时间"], "record_id_list": ["rec1"], "data": [[None, "2025-01-15T10:00:00+08:00"]]}
            return FakeProc(0, json.dumps({"ok": True, "data": page}, ensure_ascii=False))
        calls["n"] += 1
        return FakeProc(0, '{"ok": false, "error": {"message": "boom"}}')

    monkeypatch.setattr(bitable_lark, "_run", fake_run)
    monkeypatch.setattr(bitable_lark, "_has_batch_verb", lambda: "+record-batch-update")

    with pytest.raises(RuntimeError, match="全部写入失败"):
        bitable_backfill.backfill_empty_archive_dates("app", "tbl")
    assert calls["n"] == 1


def test_r9_25_empty_url_renders_plain_text() -> None:
    items = [{"feed_id": "F", "entry_key": "k", "title": "标题", "url": "", "published_at": None}]
    content = feishu_card.build_card(items, 0, ["F"])["card"]["elements"][0]["text"]["content"]
    assert "[](" not in content and "标题" in content


def test_r9_28_no_detail_url_no_webnote() -> None:
    items = [
        {"feed_id": "F", "entry_key": f"k{i}", "title": f"T{i}", "url": f"https://e.com/{i}", "published_at": None}
        for i in range(5)
    ]
    content = feishu_card.build_card(items, 0, ["F"], top_n=1)["card"]["elements"][0]["text"]["content"]
    assert "还有" in content and "详情见多维表格" not in content


def test_r9_32_topic_missing_token_rc2(monkeypatch, caplog) -> None:
    import logging

    from feedkicker import config as config_mod
    from feedkicker.config_models import Config

    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: Config(app_env="test"))
    monkeypatch.setattr(topic, "fetch_topic_fields", lambda *a: [])
    with caplog.at_level(logging.ERROR):
        rc = topic.main(["--check-fields"])
    assert rc == 2


def test_r9_34_iter_record_pages_shared(monkeypatch) -> None:
    seen: list[int] = []

    def fetch(offset: int):
        seen.append(offset)
        rows = [{"record_id": f"rec{offset + i}"} for i in range(200)]
        return FakeProc(0, json.dumps({"data": {"records": rows}}, ensure_ascii=False))

    gen = bitable_lark.iter_record_pages(fetch)
    next(gen)
    next(gen)
    assert seen == [0, 200]
