"""F30：提炼提示词文件/批量注入/JSON 解析/多源合并（全离线）。"""

from __future__ import annotations

import json

import pytest

from feedkicker.config_models import PROJECT_ROOT
from feedkicker.extract_llm import build_batch_prompt, merge_topics, parse_topics
from feedkicker.extract_parse import strip_reasoning

_PROMPT_FILE = PROJECT_ROOT / "prompts" / "extract.md"


def _topic(name="话题A", tool="工具X", principle="原理Y", links=None, sources=None) -> dict:
    return {
        "话题名称": name,
        "可使用工具": tool,
        "相关AI原理": principle,
        "资讯链接": links if links is not None else ["https://a/1"],
        "出处来源": sources if sources is not None else ["量子位"],
    }


def test_prompt_file_contains_user_prompt_and_schema() -> None:
    text = _PROMPT_FILE.read_text(encoding="utf-8")

    assert "如果用户提出提炼话题" in text
    assert "根据需求进行话题提炼，提炼必须要包含" in text
    assert "话题提炼需要过滤无工具的纯新闻信息和营销信息" in text
    assert "如果无法提炼明确的可使用工具、系统、平台、应用等信息，则跳过此条AI资讯" in text
    assert "必须提供完整的数据列表，在用户确认后，才能进行多维表格写操作" in text
    assert "不得是评测基准 / 榜单 / 数据集 / 论文" in text
    assert '"topics"' in text and "话题名称" in text and "资讯链接" in text and "出处来源" in text


def test_build_batch_prompt_injects_numbered_items() -> None:
    items = [
        {"feed_id": "量子位", "title": " 标题一 ", "url": "https://a/1", "description": "摘要一"},
        {"feed_id": "InfoQ", "title": "标题二", "url": "https://b/2", "description": ""},
    ]

    out = build_batch_prompt("模板正文", items)

    assert out.startswith("模板正文")
    assert "1. [量子位] 标题一" in out
    assert "   链接: https://a/1" in out
    assert "   摘要: 摘要一" in out
    assert "2. [InfoQ] 标题二" in out
    assert "摘要:" not in out.split("2. [InfoQ] 标题二")[1]


def test_build_batch_prompt_truncates_long_summary() -> None:
    out = build_batch_prompt("t", [{"title": "x", "url": "u", "description": "字" * 400}])

    summary_line = [ln for ln in out.splitlines() if ln.startswith("   摘要: ")][0]
    assert len(summary_line) == len("   摘要: ") + 300 + 1
    assert summary_line.endswith("…")


def test_parse_topics_normal() -> None:
    raw = json.dumps({"topics": [_topic()]}, ensure_ascii=False)

    got, dropped = parse_topics(raw)

    assert got == [
        {
            "话题名称": "话题A",
            "可使用工具": "工具X",
            "相关AI原理": "原理Y",
            "资讯链接": ["https://a/1"],
            "出处来源": ["量子位"],
        }
    ]
    assert dropped == 0


def test_parse_topics_empty_list() -> None:
    assert parse_topics('{"topics": []}') == ([], 0)


def test_parse_topics_bad_json_raises() -> None:
    with pytest.raises(ValueError):
        parse_topics("抱歉，我无法提炼")
    with pytest.raises(ValueError):
        parse_topics('{"topics": [坏数据}')


def test_parse_topics_fenced_json() -> None:
    inner = json.dumps({"topics": [_topic()]}, ensure_ascii=False)

    got, dropped = parse_topics(f"```json\n{inner}\n```")

    assert len(got) == 1 and got[0]["话题名称"] == "话题A" and dropped == 0


def test_parse_topics_with_surrounding_text() -> None:
    inner = json.dumps({"topics": [_topic()]}, ensure_ascii=False)

    got, _dropped = parse_topics(f"好的，结果如下：\n{inner}\n请确认。")

    assert len(got) == 1


def test_parse_topics_drops_bad_items_keeps_rest() -> None:
    bad = {"话题名称": "A", "可使用工具": "T", "相关AI原理": "P", "资讯链接": []}
    raw = json.dumps({"topics": [bad, "字符串", _topic("好话题")]}, ensure_ascii=False)

    got, dropped = parse_topics(raw)

    assert [t["话题名称"] for t in got] == ["好话题"]
    assert dropped == 2


def test_parse_topics_all_bad_items_returns_empty_dropped() -> None:
    bad = {"话题名称": "A", "可使用工具": "T", "相关AI原理": "P", "资讯链接": []}

    got, dropped = parse_topics(json.dumps({"topics": [bad, "字符串"]}, ensure_ascii=False))

    assert got == [] and dropped == 2


def test_parse_topics_topics_not_list_raises() -> None:
    with pytest.raises(ValueError):
        parse_topics('{"topics": {"a": 1}}')


def test_parse_topics_str_links_normalized_and_deduped() -> None:
    raw = json.dumps(
        {"topics": [_topic(links="https://a/1", sources="量子位")]}, ensure_ascii=False
    )

    got, _dropped = parse_topics(raw)

    assert got[0]["资讯链接"] == ["https://a/1"]
    assert got[0]["出处来源"] == ["量子位"]


def test_merge_topics_multi_source_dedup() -> None:
    merged = merge_topics(
        [
            _topic(links=["https://a/1"], sources=["量子位"]),
            _topic(tool="", principle="", links=["https://b/2", "https://a/1"], sources=["InfoQ", "量子位"]),
        ]
    )

    assert len(merged) == 1
    assert merged[0]["可使用工具"] == "工具X"
    assert merged[0]["相关AI原理"] == "原理Y"
    assert merged[0]["资讯链接"] == ["https://a/1", "https://b/2"]
    assert merged[0]["出处来源"] == ["量子位", "InfoQ"]


def test_merge_topics_nfkc_casefold_name_keys() -> None:
    merged = merge_topics(
        [
            _topic(name=" GPT-5 ", links=["https://a/1"]),
            _topic(name="gpt-5", links=["https://b/2"]),
            _topic(name="ＧＰＴ－５", links=["https://c/3"]),
        ]
    )

    assert len(merged) == 1
    assert merged[0]["话题名称"] == "GPT-5"
    assert merged[0]["资讯链接"] == ["https://a/1", "https://b/2", "https://c/3"]


def test_parse_topics_merges_same_name_across_items() -> None:
    raw = json.dumps(
        {
            "topics": [
                _topic(links=["https://a/1"], sources=["量子位"]),
                _topic(tool="", links=["https://a/1", "https://b/2"], sources=["量子位", "InfoQ"]),
            ]
        },
        ensure_ascii=False,
    )

    got, _dropped = parse_topics(raw)

    assert len(got) == 1
    assert got[0]["资讯链接"] == ["https://a/1", "https://b/2"]
    assert got[0]["出处来源"] == ["量子位", "InfoQ"]


def test_parse_topics_strips_think_block_with_braces() -> None:
    inner = json.dumps({"topics": [_topic(name="X", tool="T", principle="P")]}, ensure_ascii=False)
    raw = '<think>推理里含 {"示例":1} 与 {a: [1,2]} 花括号</think>\n' + inner

    got, dropped = parse_topics(raw)

    assert len(got) == 1
    assert got[0]["话题名称"] == "X"
    assert dropped == 0


def test_parse_topics_think_case_insensitive_and_consecutive() -> None:
    inner = json.dumps({"topics": [_topic()]}, ensure_ascii=False)
    raw = (
        f"<Think>第一段 {json.dumps({'a': 1})}</Think>"
        f"<THINKING>第二段 {json.dumps({'b': 2})}</THINKING>\n{inner}"
    )

    got, dropped = parse_topics(raw)

    assert len(got) == 1
    assert dropped == 0


def test_parse_topics_think_after_json() -> None:
    assert parse_topics('{"topics": []}\n<think>{"x": 1}</think>') == ([], 0)


def test_parse_topics_fenced_json_after_think() -> None:
    inner = json.dumps({"topics": [_topic()]}, ensure_ascii=False)
    raw = f"<think>{json.dumps({'示例': 1})}</think>\n```json\n{inner}\n```"

    got, dropped = parse_topics(raw)

    assert len(got) == 1
    assert dropped == 0


def test_parse_topics_unclosed_think_raises() -> None:
    with pytest.raises(ValueError):
        parse_topics("<think>只有推理没有闭合")


def test_strip_reasoning_paired_blocks() -> None:
    assert strip_reasoning('<think>a{1}</think>{"topics":[]}') == '{"topics":[]}'


def test_strip_reasoning_unclosed_truncates() -> None:
    assert strip_reasoning("keep<thinking>reason {") == "keep"


def test_strip_reasoning_no_tags_unchanged() -> None:
    text = '前置 {"topics": []}'

    assert strip_reasoning(text) == text


def test_strip_reasoning_odd_quote_keeps_json() -> None:
    raw = '<think>He said "hi and left</think>{"topics": []}'

    assert strip_reasoning(raw) == '{"topics": []}'


def test_strip_reasoning_escaped_quote_keeps_json() -> None:
    raw = '<think>a \\"b</think>{"topics": []}'

    assert strip_reasoning(raw) == '{"topics": []}'


def test_strip_reasoning_mixed_nested_tags_keeps_json() -> None:
    raw = '<think>a<thinking>b</thinking>c</think>{"topics": []}'

    assert strip_reasoning(raw) == '{"topics": []}'


def test_prompt_file_contains_human_score_filter_clauses() -> None:
    text = _PROMPT_FILE.read_text(encoding="utf-8")

    assert "受众定位过滤（toB / 开发者 / 通用职场）" in text
    assert "跨行业普适过滤" in text
    assert "现场可达性过滤" in text
    assert "「被提及」≠「可提炼」" in text
    assert "信源可核实过滤" in text
    assert "正反例（取自人工评审真实判定" in text
    assert "## 已有话题（避免重复）" in text


def test_build_batch_prompt_injects_existing_topics_section() -> None:
    items = [{"feed_id": "源", "title": "标题", "url": "https://a/1", "description": ""}]

    out = build_batch_prompt("模板正文", items, ["已存在话题 A", " 已存在话题 B "])

    head, tail = out.split("## 本批资讯", 1)
    assert "## 已有话题（避免重复）" in head
    assert "- 已存在话题 A" in head and "- 已存在话题 B" in head
    assert "标题" in tail


def test_build_batch_prompt_omits_existing_section_when_empty() -> None:
    out_none = build_batch_prompt("t", [], None)
    out_empty = build_batch_prompt("t", [], ["  ", ""])

    assert "## 已有话题" not in out_none
    assert "## 已有话题" not in out_empty
    assert "## 本批资讯" in out_none


def test_build_batch_prompt_caps_existing_topics_at_200() -> None:
    out = build_batch_prompt("t", [], [f"话题{i}" for i in range(205)])

    section = out.split("## 本批资讯")[0]
    assert section.count("\n- ") == 200
    assert "话题204" not in section


def test_prompt_file_contains_exemption_clauses() -> None:
    text = _PROMPT_FILE.read_text(encoding="utf-8")

    assert "豁免（职场效率 Agent）" in text
    assert "豁免 A（开源 / 端侧模型本地落地）" in text
    assert "豁免 B（可动手复现的研究）" in text
    assert "豁免 C（事件承载的选型 / 成本工程）" in text
    assert "豁免不适用的情形" in text
    assert "仅在发布会上「宣布开源" in text
