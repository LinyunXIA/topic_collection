"""Round 10 P3（#389，R10-08..R10-36）验收：解析/去重/bitable/salon 边界与文档同步（全离线）。"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from feedkicker import (
    bitable_backfill,
    bitable_lark,
    bitable_purge,
    bitable_views,
    extract_write,
    minimax_parse,
    purge,
    reasoning,
    score_parse,
    score_write,
    store,
    topic,
    wiki_lark,
)
from feedkicker.config_models import Config, SalonConf, WikiConf
from feedkicker.fetch import dedup_key, trim_url
from feedkicker.minimax_transport import content_blocks_text

_DIMS = dict.fromkeys(
    ("普适痛点强度", "分层承载力", "可演示性", "时效与稀缺", "内容复用价值", "讲解成本"), 4.0
)


class FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _item(name: str = "A", **over) -> dict:
    item = {
        "record_id": f"rec{name}", "话题名称": name, "weighted_total": 4.0,
        "scores": dict(_DIMS), "reason": "依据 可使用工具", "risk_flag": False, "source_flag": False,
    }
    item.update(over)
    return item


def _doc(name: str) -> str:
    from feedkicker.config_models import PROJECT_ROOT

    return (PROJECT_ROOT / name).read_text(encoding="utf-8")


# ── score ──


def test_r10_08_string_false_flags_normalized() -> None:
    norm = score_parse._normalize_item(_item("A", risk_flag="false", source_flag="0"), {"record_id": "recA", "话题名称": "A"})

    assert norm["risk_flag"] is False and norm["source_flag"] is False
    detail = score_write._cell(norm, "MMax")["MMax理由"]
    assert "risk=false" in detail and "source=false" in detail


def test_r10_09_record_id_only_disambiguates_within_name() -> None:
    rows = [{"record_id": "rec1", "话题名称": "A"}, {"record_id": "rec2", "话题名称": "B"}]

    normalized, _dist, dropped = score_parse.normalize_results([_item("A", record_id="rec2")], rows)

    assert len(normalized) == 1 and normalized[0]["record_id"] == "rec1" and dropped == 1


def test_r10_09_record_id_disambiguates_same_name() -> None:
    rows = [{"record_id": "rec1", "话题名称": "同名"}, {"record_id": "rec2", "话题名称": "同名"}]

    normalized, _dist, dropped = score_parse.normalize_results(
        [_item("同名", record_id="rec2"), _item("同名")], rows
    )

    assert {n["record_id"] for n in normalized} == {"rec1", "rec2"} and dropped == 0


def test_r10_10_single_risk_source_form() -> None:
    norm = score_parse._normalize_item(_item("A", risk_flag=True, source_flag=True), {"record_id": "recA", "话题名称": "A"})

    assert "｜risk" not in norm["reason"] and "｜source" not in norm["reason"]
    detail = score_write._cell(norm, "MMax")["MMax理由"]
    assert detail.count("risk=") == 1 and detail.count("source=") == 1


def test_r10_11_design_no_per_row_fallback() -> None:
    section = _doc("docs/DESIGN.md").split("### 26.5 写入格式", 1)[1].split("### 26.6", 1)[0]

    assert "回退逐条" not in section
    assert "缺" in section and "raise" in section


def test_r10_12_rc1_includes_all_llm_batches_failed() -> None:
    cli = _doc("docs/CLI.md").split("## tc-score", 1)[1].split("\n## ", 1)[0]
    design = _doc("docs/DESIGN.md")

    assert "全部 LLM 批失败" in cli
    assert "全部 LLM 批失败" in design


def test_r10_13_max_calls_zero_semantics_documented() -> None:
    cli = _doc("docs/CLI.md").split("## tc-score", 1)[1].split("\n## ", 1)[0]
    prd = _doc("docs/PRD.md")

    assert "非 0" in cli and "score.max_calls" in cli
    assert "非 0" in prd


# ── 解析 / 共享层 ──


def test_r10_14_unclosed_attributed_tags_linear() -> None:
    text = "<think foo=1>" * 20000
    start = time.monotonic()
    reasoning.strip_reasoning(text)

    assert time.monotonic() - start < 1.0


def test_r10_14_behavior_preserved() -> None:
    assert reasoning.strip_reasoning('<think>a{1}</think>{"topics":[]}') == '{"topics":[]}'
    assert reasoning.strip_reasoning('keep<thinking>reason {') == "keep"
    assert reasoning.strip_reasoning('<think foo=1>unclosed{"a":1}') == '<think foo=1>unclosed{"a":1}'


def test_r10_15_minimax_parse_prose_and_uppercase_fence() -> None:
    outline = {"slides": [{"bullets": ["x"]}]}
    body = json.dumps(outline, ensure_ascii=False)
    for content in (f"好的，以下是：\n```json\n{body}\n```\n请查收", f"```JSON\n{body}\n```"):
        assert minimax_parse.parse_outline_from_response({"choices": [{"message": {"content": content}}]}) == outline


def test_r10_16_non_str_topic_fields_dropped() -> None:
    from feedkicker.extract_parse import parse_topics

    raw = json.dumps(
        {"topics": [{"话题名称": ["a", "b"], "可使用工具": {"x": 1}, "相关AI原理": 2, "资讯链接": "u", "出处来源": ["s"]}]},
        ensure_ascii=False,
    )

    got, dropped = parse_topics(raw)

    assert got == [] and dropped == 1


def test_r10_17_content_blocks_empty_text_falls_back() -> None:
    assert content_blocks_text([{"text": "", "content": "hello"}]) == "hello"
    assert content_blocks_text([{"text": "he"}, {"content": "llo"}]) == "hello"


def test_r10_18_dead_link_key_removed() -> None:
    assert not hasattr(extract_write, "_link_key")


# ── 去重 / wiki ──


def test_r10_19_hash_route_non_root_path_distinct() -> None:
    assert dedup_key("https://spa.example/app#/post/1") != dedup_key("https://spa.example/app#/post/2")
    assert dedup_key("https://spa.example/page#section") == dedup_key("https://spa.example/page")


def test_r10_20_query_tail_punctuation_kept() -> None:
    assert trim_url("https://e.com/search?q=a!") == "https://e.com/search?q=a!"
    assert dedup_key("https://e.com/search?q=a!") != dedup_key("https://e.com/search?q=a")
    assert trim_url("(https://e.com/a)") == "https://e.com/a"
    assert trim_url("<https://e.com/a>") == "https://e.com/a"
    assert trim_url("https://e.com/a_(b_(c))") == "https://e.com/a_(b_(c))"


def test_r10_21_wiki_node_list_page_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        wiki_lark.bitable_lark,
        "_run",
        lambda args, stdin_text=None, timeout=120: calls.append(list(args)) or FakeProc(0, '{"ok":true,"data":{"nodes":[]}}'),
    )

    wiki_lark.lark_node_list("spc", "parent")

    assert calls[0][:2] == ["wiki", "+node-list"]
    assert "--page-limit" in calls[0] and calls[0][calls[0].index("--page-limit") + 1] == "0"


# ── bitable / purge ──


@pytest.mark.parametrize("bad", ["2026", "12345678", "20261301", "20261301120000", "0", "1", "999999999"])
def test_r10_22_implausible_numeric_returns_none(bad: str) -> None:
    assert bitable_backfill._shanghai_date(bad) is None


def test_r10_22_valid_forms_still_parse() -> None:
    assert bitable_backfill._shanghai_date("20260101") == "2026-01-01"
    expected = datetime.fromtimestamp(1682899200, tz=UTC).astimezone(bitable_lark.SHANGHAI).strftime("%Y-%m-%d")
    assert bitable_backfill._shanghai_date("1682899200") == expected
    assert bitable_backfill._shanghai_date("1682899200000") == expected


def test_r10_23_fields_without_data_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    body = json.dumps({"ok": True, "data": {"fields": ["推送时间", "归档日期"]}}, ensure_ascii=False)
    monkeypatch.setattr(bitable_lark, "_run", lambda *a, **k: FakeProc(0, body))

    with pytest.raises(RuntimeError):
        bitable_purge._list_records("app", "tbl")


def test_r10_24_backfill_record_id_list_only_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bitable_lark, "_has_batch_verb", lambda: "+record-batch-update")
    body = json.dumps({"ok": True, "data": {"record_id_list": ["rec1"]}}, ensure_ascii=False)
    monkeypatch.setattr(bitable_lark, "_run", lambda *a, **k: FakeProc(0, body))

    with pytest.raises(RuntimeError, match="record_id"):
        bitable_backfill.backfill_empty_archive_dates("app", "tbl")


def test_r10_25_backfill_partial_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def rows(base: int) -> list[dict]:
        return [
            {"record_id": f"rec{base + i}", "fields": {"推送时间": "2025-01-15T10:00:00+08:00", "归档日期": ""}}
            for i in range(200)
        ]

    state = {"write": 0}

    def fake_run(args, stdin_text=None, timeout=120):
        if args[:2] == ["base", "--help"]:
            return FakeProc(0, "+record-batch-update")
        if "+record-list" in args:
            off = int(args[args.index("--offset") + 1])
            page = rows(0) if off == 0 else (rows(200) if off == 200 else [])
            return FakeProc(0, json.dumps({"ok": True, "data": {"records": page}}, ensure_ascii=False))
        if "+record-batch-update" in args:
            state["write"] += 1
            return FakeProc(0, json.dumps({"ok": state["write"] == 1}, ensure_ascii=False))
        return FakeProc(0, "{}")

    monkeypatch.setattr(bitable_lark, "_run", fake_run)

    with pytest.raises(RuntimeError, match="部分"):
        bitable_backfill.backfill_empty_archive_dates("app", "tbl")


def test_r10_26_purge_same_base_guard() -> None:
    cfg = Config(app_env="test")
    cfg.bitable.enabled = True
    cfg.bitable.app_token = cfg.salon.app_token = "appSame"
    cfg.bitable.table_id = cfg.salon.table_id = "tblSame"
    conn = store.connect(":memory:")

    stats = purge.run(cfg, conn, dry_run=True)

    assert "salon" in stats.bitable_skipped_reason
    conn.close()


def test_r10_26_reseed_same_base_rc2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from feedkicker import bitable

    cfg = Config(app_env="test", db_path=tmp_path / "t.db")
    cfg.bitable.enabled = True
    cfg.bitable.app_token = cfg.salon.app_token = "appSame"
    cfg.bitable.table_id = cfg.salon.table_id = "tblSame"
    monkeypatch.setattr("feedkicker.config.load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(bitable_lark, "lark_bin", lambda: "/fake/lark-cli")

    assert bitable.main(["--reseed", "--env", "test"]) == 2


def test_r10_27_create_date_view_missing_id_no_default_override(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(args, stdin_text=None, timeout=120):
        calls.append(list(args))
        if args[:2] == ["base", "+view-list"]:
            return FakeProc(0, json.dumps({"ok": True, "data": {"views": [{"view_name": "表格", "id": "vewDefault"}]}}, ensure_ascii=False))
        if args[:2] == ["base", "+view-create"]:
            return FakeProc(0, json.dumps({"ok": True, "data": {"view": {}}}, ensure_ascii=False))
        return FakeProc(0, "{}")

    monkeypatch.setattr(bitable_lark, "_run", fake_run)

    assert bitable_views.create_date_view("app", "tbl") is False
    assert not any("+view-set-group" in c or "+view-set-sort" in c for c in calls)


def test_r10_28_topic_field_list_paginates(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = [[{"field_name": f"f{i}"} for i in range(200)], [{"field_name": "讨论状态", "type": "select"}]]

    def fake_run(args, stdin_text=None, timeout=60):
        off = int(args[args.index("--offset") + 1]) if "--offset" in args else 0
        return FakeProc(0, json.dumps({"ok": True, "data": {"fields": pages[off // 200]}}, ensure_ascii=False))

    monkeypatch.setattr(topic.bitable_lark, "_run", fake_run)

    fields = topic.fetch_topic_fields("app", "tbl")

    assert len(fields) == 201 and fields[-1]["field_name"] == "讨论状态"


def test_r10_28_archive_field_paginates(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = [[{"field_name": f"f{i}"} for i in range(200)], [{"field_name": "归档日期"}]]
    calls: list[list[str]] = []

    def fake_run(args, stdin_text=None, timeout=60):
        calls.append(list(args))
        if "+field-create" in args:
            return FakeProc(0, "{}")
        off = int(args[args.index("--offset") + 1]) if "--offset" in args else 0
        return FakeProc(0, json.dumps({"ok": True, "data": {"fields": pages[off // 200]}}, ensure_ascii=False))

    monkeypatch.setattr(bitable_lark, "_run", fake_run)

    assert bitable_views.ensure_archive_date_field("app", "tbl") is True
    assert not any("+field-create" in c for c in calls)


# ── topic / salon ──


def test_r10_29_placeholder_token_rc2(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Config(app_env="test")
    cfg.salon.app_token = "<salon-app-token>"
    cfg.salon.table_id = "<salon-table-id>"
    monkeypatch.setattr("feedkicker.config.load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(topic, "fetch_topic_fields", lambda *a: (_ for _ in ()).throw(AssertionError("占位 token 不得访问")))

    assert topic.main(["--check-fields"]) == 2


def test_r10_30_mark_failure_counted_rc1(monkeypatch: pytest.MonkeyPatch) -> None:
    from feedkicker import salon_flow as sf

    cfg = Config(app_env="test")
    cfg.salon = SalonConf(enabled=True, app_token="app", table_id="tbl", wiki_space_id="spc", wiki_parent_token="par")
    cfg.wiki = WikiConf(space_id="spc", parent_token="par", app_token="app")
    conn = store.connect(":memory:")
    monkeypatch.setattr(
        sf, "fetch_selected_topics",
        lambda *a, **k: [{"record_id": "rec1", "fields": {"讨论状态": ["已选题"], "话题名称": "T1"}}],
    )
    monkeypatch.setattr(sf.minimax, "gen_outline", lambda *a, **k: {"title": "t", "slides": [{"heading": "h", "bullets": ["a"]}]})
    monkeypatch.setattr(sf.wiki, "create_wiki_doc_from_md", lambda *a, **k: "https://x/wiki/1")
    monkeypatch.setattr(sf.salon_notify, "send_wiki_card", lambda *a, **k: True)
    monkeypatch.setattr(sf.wiki_home, "update_homepage", lambda *a, **k: True)
    monkeypatch.setattr(store, "mark_topic_archived", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db boom")))

    assert sf.run(cfg, conn, dry_run=False) == 1
    conn.close()


# ── 文档 / 卫生 ──


def test_r10_31_salon_exit_code_documented() -> None:
    section = _doc("docs/DESIGN.md").split("### 24.1", 1)[1].split("### 24.2", 1)[0]
    cli = _doc("docs/CLI.md").split("## tc-salon", 1)[1].split("\n## ", 1)[0]

    assert "rc 仍 0" not in section and "salon 全失败返回码" in section and "rc 1" in section
    assert "全部失败也仅 WARNING" not in cli and "rc `1`" in cli


def test_r10_32_cli_exit_codes_match() -> None:
    cli = _doc("docs/CLI.md")
    bitable_section = cli.split("## feedkicker.bitable", 1)[1].split("\n## ", 1)[0]
    topic_section = cli.split("## feedkicker.topic", 1)[1].split("\n## ", 1)[0]

    assert "rc 2" in bitable_section or "`2`" in bitable_section
    assert "rc 2" in topic_section or "`2`" in topic_section


def test_r10_33_ops_heading_unique() -> None:
    ops = _doc("docs/OPS.md")
    assert ops.count("### 3.3") == 1 and "### 3.4" in ops


def test_r10_34_ops_batch_size_lower_bound() -> None:
    ops = _doc("docs/OPS.md")
    assert "下界静默钳 1" in ops


def test_r10_35_design_registers_pagination_helpers() -> None:
    design = _doc("docs/DESIGN.md")
    assert "iter_record_pages" in design and "guard_pages" in design


def test_r10_36_readme_self_check_under_3s() -> None:
    readme = _doc("README.md")
    assert "<1s" not in readme and "<3s" in readme
