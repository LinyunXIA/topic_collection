"""F31：字段映射 / 「资讯链接 OR 话题名称」双键去重跳过 / dry-run 零写 / ≤200 分批（全 mock 离线）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from feedkicker import bitable_lark
from feedkicker.config_models import ExtractConf, ProviderConf
from feedkicker.extract_llm import resolve_provider
from feedkicker.extract_write import (
    build_record,
    existing_index,
    link_keys,
    write_topics,
)


class FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _json_from_args(args: list[str]) -> dict[str, Any]:
    raw = args[args.index("--json") + 1]
    if raw.startswith("@"):
        return json.loads(Path(raw[1:]).read_text(encoding="utf-8"))
    return json.loads(raw)


def _records_resp(names: list[str], start: int = 0) -> FakeProc:
    records = [
        {"record_id": f"rec{start + i}", "fields": {"话题名称": name}} for i, name in enumerate(names)
    ]
    return FakeProc(0, json.dumps({"data": {"records": records}}, ensure_ascii=False))


def _link_records_resp(links: list[str]) -> FakeProc:
    records = [
        {"record_id": f"lrec{i}", "fields": {"资讯链接": link}} for i, link in enumerate(links)
    ]
    return FakeProc(0, json.dumps({"data": {"records": records}}, ensure_ascii=False))


def _topic(name: str, links: list[str] | None = None, sources: list[str] | None = None) -> dict:
    return {
        "话题名称": name,
        "可使用工具": f"工具-{name}",
        "相关AI原理": f"原理-{name}",
        "资讯链接": links or [f"https://e/{name}"],
        "出处来源": sources or ["量子位"],
    }


def test_dry_run_zero_write_calls(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(args, stdin_text=None, timeout=120):
        calls.append(list(args))
        return _records_resp([])

    monkeypatch.setattr(bitable_lark, "_run", fake_run)

    assert write_topics("app", "tbl", [_topic("A"), _topic("B")], "MMax", "2026-09-14", dry_run=True) == (2, 0)
    assert [c for c in calls if "+record-batch-create" in c] == []
    assert any("+record-list" in c for c in calls)


def test_apply_skips_existing_topic_names(monkeypatch) -> None:
    created: list[list[dict]] = []

    def fake_run(args, stdin_text=None, timeout=120):
        if "+record-list" in args:
            return _records_resp(["话题A"])
        if "+record-batch-create" in args:
            created.append(_json_from_args(args)["create_records"])
            return FakeProc(0, "{}")
        return FakeProc(0, "{}")

    monkeypatch.setattr(bitable_lark, "_run", fake_run)

    assert write_topics("app", "tbl", [_topic("话题A"), _topic("话题B")], "MMax", "2026-09-14") == (1, 1)
    assert [[r["话题名称"] for r in chunk] for chunk in created] == [["话题B"]]


def test_apply_in_batch_duplicate_skipped(monkeypatch) -> None:
    created: list[list[dict]] = []

    def fake_run(args, stdin_text=None, timeout=120):
        if "+record-list" in args:
            return _records_resp([])
        created.append(_json_from_args(args)["create_records"])
        return FakeProc(0, "{}")

    monkeypatch.setattr(bitable_lark, "_run", fake_run)

    assert write_topics("app", "tbl", [_topic("A"), _topic("A"), _topic("B")], "MMax", "2026-09-14") == (2, 1)
    assert [r["话题名称"] for r in created[0]] == ["A", "B"]


def test_apply_skips_when_link_matches_existing(monkeypatch) -> None:
    created: list[list[dict]] = []
    calls: list[list[str]] = []

    def fake_run(args, stdin_text=None, timeout=120):
        calls.append(list(args))
        if "+record-list" in args:
            return _link_records_resp(["https://E.com/a#frag"])
        created.append(_json_from_args(args)["create_records"])
        return FakeProc(0, "{}")

    monkeypatch.setattr(bitable_lark, "_run", fake_run)

    topic = {"话题名称": "全新命名", "资讯链接": ["https://e.com/a"]}
    assert write_topics("app", "tbl", [topic], "MMax", "2026-09-14") == (0, 1)
    assert created == []
    assert "--field-id" in calls[0]


def test_apply_writes_when_both_keys_miss(monkeypatch) -> None:
    created: list[list[dict]] = []

    def fake_run(args, stdin_text=None, timeout=120):
        if "+record-list" in args:
            return FakeProc(
                0,
                json.dumps(
                    {
                        "data": {
                            "records": [
                                {"record_id": "r1", "fields": {"话题名称": "其他话题"}},
                                {"record_id": "r2", "fields": {"资讯链接": "https://other/x"}},
                            ]
                        }
                    },
                    ensure_ascii=False,
                ),
            )
        created.append(_json_from_args(args)["create_records"])
        return FakeProc(0, "{}")

    monkeypatch.setattr(bitable_lark, "_run", fake_run)

    topic = {"话题名称": "新话题", "资讯链接": ["https://new/1"]}
    assert write_topics("app", "tbl", [topic], "MMax", "2026-09-14") == (1, 0)
    assert [r["话题名称"] for r in created[0]] == ["新话题"]


def test_apply_in_batch_same_link_writes_once(monkeypatch) -> None:
    created: list[list[dict]] = []

    def fake_run(args, stdin_text=None, timeout=120):
        if "+record-list" in args:
            return _records_resp([])
        created.append(_json_from_args(args)["create_records"])
        return FakeProc(0, "{}")

    monkeypatch.setattr(bitable_lark, "_run", fake_run)

    topics = [
        {"话题名称": "命名甲", "资讯链接": ["https://same/1"]},
        {"话题名称": "命名乙", "资讯链接": ["https://same/1"]},
    ]
    assert write_topics("app", "tbl", topics, "MMax", "2026-09-14") == (1, 1)
    assert [r["话题名称"] for r in created[0]] == ["命名甲"]


def test_apply_field_mapping(monkeypatch) -> None:
    created: list[dict] = []
    calls: list[list[str]] = []

    def fake_run(args, stdin_text=None, timeout=120):
        calls.append(list(args))
        if "+record-list" in args:
            return _records_resp([])
        created.extend(_json_from_args(args)["create_records"])
        return FakeProc(0, "{}")

    monkeypatch.setattr(bitable_lark, "_run", fake_run)

    topic = _topic("话题A", links=["https://a/1", "https://b/2"], sources=["量子位", "InfoQ"])
    write_topics("app", "tbl", [topic], "MMax", "2026-09-14")

    assert created == [
        {
            "话题名称": "话题A",
            "可使用工具": "工具-话题A",
            "相关AI原理": "原理-话题A",
            "资讯链接": "https://a/1\nhttps://b/2",
            "出处来源": "量子位\nInfoQ",
            "提炼日期": "2026-09-14",
            "讨论状态": ["未讨论"],
            "提取工具": ["MMax"],
        }
    ]
    assert [c for c in calls if "+field-list" in c] == []


def test_build_record_newline_join_and_dedup() -> None:
    rec = build_record(
        _topic("A", links=["https://a/1", "https://a/1", "https://b/2"], sources=["量子位"]),
        "DS",
        "2026-09-14",
    )

    assert rec["资讯链接"] == "https://a/1\nhttps://b/2"
    assert rec["提取工具"] == ["DS"]
    assert rec["讨论状态"] == ["未讨论"]


def test_apply_writes_status_as_single_element_array(monkeypatch) -> None:
    created: list[list[dict]] = []
    calls: list[list[str]] = []

    def fake_run(args, stdin_text=None, timeout=120):
        calls.append(list(args))
        if "+record-list" in args:
            return _records_resp([])
        created.append(_json_from_args(args)["create_records"])
        return FakeProc(0, "{}")

    monkeypatch.setattr(bitable_lark, "_run", fake_run)

    assert write_topics("app", "tbl", [_topic("A")], "MMax", "2026-09-14") == (1, 0)
    assert created[0][0]["讨论状态"] == ["未讨论"]
    assert [c for c in calls if "+field-list" in c] == []


def test_batch_create_chunks_at_200(monkeypatch) -> None:
    chunks: list[int] = []

    def fake_run(args, stdin_text=None, timeout=120):
        if "+record-list" in args:
            return _records_resp([])
        chunks.append(len(_json_from_args(args)["create_records"]))
        return FakeProc(0, "{}")

    monkeypatch.setattr(bitable_lark, "_run", fake_run)

    topics = [_topic(f"话题{i}") for i in range(205)]
    assert write_topics("app", "tbl", topics, "MMax", "2026-09-14") == (205, 0)
    assert chunks == [200, 5]


def test_partial_chunk_failure_counts_success(monkeypatch) -> None:
    calls = {"n": 0}

    def fake_run(args, stdin_text=None, timeout=120):
        if "+record-list" in args:
            return _records_resp([])
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeProc(0, "{}")
        return FakeProc(0, '{"ok": false, "error": {"message": "boom"}}')

    monkeypatch.setattr(bitable_lark, "_run", fake_run)

    topics = [_topic(f"话题{i}") for i in range(205)]
    assert write_topics("app", "tbl", topics, "MMax", "2026-09-14") == (200, 0)
    assert calls["n"] == 2


def test_existing_index_records_and_fields_data_shapes(monkeypatch) -> None:
    def fake_run_records(args, stdin_text=None, timeout=120):
        return _records_resp(["话题A", "话题B"])

    monkeypatch.setattr(bitable_lark, "_run", fake_run_records)
    assert existing_index("app", "tbl") == ({"话题a", "话题b"}, set())

    def fake_run_fields_data(args, stdin_text=None, timeout=120):
        body = {"data": {"fields": ["话题名称", "其他"], "data": [["话题C", 1], ["话题D", 2]]}}
        return FakeProc(0, json.dumps(body, ensure_ascii=False))

    monkeypatch.setattr(bitable_lark, "_run", fake_run_fields_data)
    assert existing_index("app", "tbl") == ({"话题c", "话题d"}, set())


def test_existing_index_multi_select_value_is_flattened(monkeypatch) -> None:
    def fake_run(args, stdin_text=None, timeout=120):
        body = {"data": {"records": [{"record_id": "rec1", "fields": {"话题名称": ["话题A", "话题B"]}}]}}
        return FakeProc(0, json.dumps(body, ensure_ascii=False))

    monkeypatch.setattr(bitable_lark, "_run", fake_run)

    assert existing_index("app", "tbl") == ({"话题a", "话题b"}, set())


def test_existing_index_normalizes_names(monkeypatch) -> None:
    def fake_run(args, stdin_text=None, timeout=120):
        body = {"data": {"records": [{"record_id": "rec1", "fields": {"话题名称": " GPT-5 "}}]}}
        return FakeProc(0, json.dumps(body, ensure_ascii=False))

    monkeypatch.setattr(bitable_lark, "_run", fake_run)

    assert existing_index("app", "tbl") == ({"gpt-5"}, set())


def test_existing_index_normalizes_links(monkeypatch) -> None:
    def fake_run(args, stdin_text=None, timeout=120):
        return _link_records_resp(["https://Example.com/a#frag", " https://b.com/p?q=1 "])

    monkeypatch.setattr(bitable_lark, "_run", fake_run)

    assert existing_index("app", "tbl") == (set(), {"https://example.com/a", "https://b.com/p?q=1"})


def test_existing_index_fields_without_topic_name_raises(monkeypatch) -> None:
    def fake_run(args, stdin_text=None, timeout=120):
        body = {"data": {"fields": ["其他"], "data": [["x"]]}}
        return FakeProc(0, json.dumps(body, ensure_ascii=False))

    monkeypatch.setattr(bitable_lark, "_run", fake_run)

    with pytest.raises(RuntimeError, match="话题名称"):
        existing_index("app", "tbl")


def test_write_skips_existing_after_nfkc_normalization(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(args, stdin_text=None, timeout=120):
        calls.append(list(args))
        if "+record-list" in args:
            return _records_resp([" GPT-5 "])
        return FakeProc(0, "{}")

    monkeypatch.setattr(bitable_lark, "_run", fake_run)

    assert write_topics("app", "tbl", [_topic("gpt-5")], "MMax", "2026-09-14") == (0, 1)
    assert [c for c in calls if "+record-batch-create" in c] == []


def test_write_in_batch_dedup_after_nfkc_keeps_original_value(monkeypatch) -> None:
    created: list[list[dict]] = []

    def fake_run(args, stdin_text=None, timeout=120):
        if "+record-list" in args:
            return _records_resp([])
        created.append(_json_from_args(args)["create_records"])
        return FakeProc(0, "{}")

    monkeypatch.setattr(bitable_lark, "_run", fake_run)

    got = write_topics(
        "app", "tbl", [_topic("ＧＰＴ－５"), _topic("gpt-5")], "MMax", "2026-09-14"
    )

    assert got == (1, 1)
    assert [r["话题名称"] for r in created[0]] == ["ＧＰＴ－５"]


def test_existing_index_paginates(monkeypatch) -> None:
    offsets: list[str] = []

    def fake_run(args, stdin_text=None, timeout=120):
        offsets.append(args[args.index("--offset") + 1])
        if len(offsets) == 1:
            return _records_resp([f"话题{i}" for i in range(200)], start=0)
        return _records_resp(["话题-tail"], start=200)

    monkeypatch.setattr(bitable_lark, "_run", fake_run)

    names, links = existing_index("app", "tbl")

    assert offsets == ["0", "200"]
    assert len(names) == 201 and "话题-tail" in names
    assert links == set()


def test_existing_index_business_failure_raises(monkeypatch) -> None:
    monkeypatch.setattr(
        bitable_lark, "_run", lambda *a, **k: FakeProc(0, '{"ok": false, "error": {"message": "x"}}')
    )

    with pytest.raises(RuntimeError, match="中止写入"):
        existing_index("app", "tbl")


def test_existing_index_bad_container_raises(monkeypatch) -> None:
    monkeypatch.setattr(bitable_lark, "_run", lambda *a, **k: FakeProc(0, '{"ok": true, "data": {"records": ["坏"]}}'))

    with pytest.raises(RuntimeError):
        existing_index("app", "tbl")


def test_write_requires_tokens() -> None:
    with pytest.raises(RuntimeError, match="app_token"):
        write_topics("", "", [], "MMax", "2026-09-14")


def test_provider_tool_labels_default_by_provider() -> None:
    mm = resolve_provider(ExtractConf(provider="minimax", providers={"minimax": ProviderConf(api_key="k")}))
    ds = resolve_provider(ExtractConf(provider="deepseek", providers={"deepseek": ProviderConf(api_key="k")}))

    assert mm.tool_label == "MMax"
    assert ds.tool_label == "DS"


def test_provider_registry_tool_labels_are_table_options() -> None:
    from feedkicker.extract_llm import PROVIDERS

    assert PROVIDERS["minimax"].tool_label == "MMax"
    assert PROVIDERS["deepseek"].tool_label == "DS"


_MARKDOWN_WRAP = (
    "[https://www.ifanr.com/1678637?utm_source=rss&utm_medium=rss"
    "\nhttps://www.qbitai.com/2026/09/485431.html]"
    "(https://www.ifanr.com/1678637?utm_source=rss&utm_medium=rss"
    "\nhttps://www.qbitai.com/2026/09/485431.html)"
)


def test_link_keys_unwraps_markdown_and_splits_lines() -> None:
    assert link_keys(_MARKDOWN_WRAP) == {
        "https://www.ifanr.com/1678637",
        "https://www.qbitai.com/2026/09/485431.html",
    }


def test_link_keys_strips_tracking_and_matches_bare_candidate() -> None:
    table = "https://www.ifanr.com/1678637?utm_source=rss&utm_medium=rss"
    candidate = "https://www.ifanr.com/1678637"

    assert link_keys(table) == link_keys(candidate) == {"https://www.ifanr.com/1678637"}


def test_link_keys_keeps_meaningful_query() -> None:
    assert link_keys("https://e.com/p?id=123") == {"https://e.com/p?id=123"}
    assert link_keys("https://e.com/p?id=123") != link_keys("https://e.com/p?id=456")


def test_link_keys_normalizes_fragment_and_host_case() -> None:
    assert link_keys("https://Example.com/a#frag") == {"https://example.com/a"}


def test_apply_skips_when_markdown_wrapped_tracking_link_matches(monkeypatch) -> None:
    created: list[list[dict]] = []

    def fake_run(args, stdin_text=None, timeout=120):
        if "+record-list" in args:
            return _link_records_resp([_MARKDOWN_WRAP])
        created.append(_json_from_args(args)["create_records"])
        return FakeProc(0, "{}")

    monkeypatch.setattr(bitable_lark, "_run", fake_run)

    topic = {"话题名称": "全新命名", "资讯链接": ["https://www.ifanr.com/1678637"]}

    assert write_topics("app", "tbl", [topic], "MMax", "2026-09-14") == (0, 1)
    assert created == []


def test_existing_index_normalizes_markdown_wrapped_links(monkeypatch) -> None:
    monkeypatch.setattr(bitable_lark, "_run", lambda *a, **k: _link_records_resp([_MARKDOWN_WRAP]))

    assert existing_index("app", "tbl") == (
        set(),
        {"https://www.ifanr.com/1678637", "https://www.qbitai.com/2026/09/485431.html"},
    )
