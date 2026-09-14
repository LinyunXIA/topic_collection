"""F39：提示词模板 / 横向上文注入 / read_rows 目标列 / provider 解析（全离线）。"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest

from feedkicker import bitable_lark, extract_llm, score_flow, score_llm, score_source
from feedkicker.config_models import ProviderConf, ScoreConf

_MM_FIELDS = ["话题名称", "可使用工具", "相关AI原理", "资讯链接", "出处来源", "MMax打分", "MMax理由"]


def _stub_llm_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(conf, prompt):
        names = re.findall(r"^\d+\. 话题名称：(.+)$", prompt, re.MULTILINE)
        dims = {
            "普适痛点强度": 4.0, "分层承载力": 4.0, "可演示性": 4.0,
            "时效与稀缺": 4.0, "内容复用价值": 4.0, "讲解成本": 4.0,
        }
        results = [
            {
                "话题名称": n.strip(), "gate": "pass", "dimensions": dims, "missing": [],
                "weighted_total": 4.0,
                "risk_flag": False, "source_flag": False, "reason": "依据字段",
            }
            for n in names
        ]
        return json.dumps({"scores": results}, ensure_ascii=False)

    monkeypatch.setattr(score_llm, "call_llm", fake)


class FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _records(n: int, *, start: int = 1, score: str | None = None) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for i in range(n):
        fields: dict[str, object] = {
            "话题名称": f"话题{start + i}",
            "可使用工具": "工具X",
            "相关AI原理": "原理Y",
            "资讯链接": "https://a/1",
            "出处来源": "量子位",
        }
        if score is not None:
            fields["MMax打分"] = score
            fields["MMax理由"] = "原因"
        rows.append({"record_id": f"rec{start + i}", "fields": fields})
    return rows


def _patch_lark(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pages: list[list[dict[str, object]]] | None = None,
    field_names: list[str] | None = None,
    calls: list[list[str]] | None = None,
) -> None:
    def fake_run(args, stdin_text=None, timeout=120):
        if calls is not None:
            calls.append(list(args))
        if args[:2] == ["base", "--help"]:
            return FakeProc(0, "+record-batch-update")
        if "+record-batch-update" in args:
            return FakeProc(0, "{}")
        if "+field-list" in args:
            payload = {"fields": [{"field_name": n} for n in (field_names or [])]}
            return FakeProc(0, json.dumps({"data": payload}, ensure_ascii=False))
        if "+record-list" in args:
            offset = int(args[args.index("--offset") + 1])
            idx = offset // bitable_lark._CHUNK
            page = pages[idx] if pages and idx < len(pages) else []
            return FakeProc(0, json.dumps({"data": {"records": page}}, ensure_ascii=False))
        return FakeProc(0, "{}")

    monkeypatch.setattr(bitable_lark, "_run", fake_run)


def _write_cfg(
    tmp_path: Path, *, provider: str = "minimax", key: str = "sk-test", prompt_file: str = "prompts/score.md"
) -> Path:
    path = tmp_path / "f39-cfg.yaml"
    path.write_text(
        "salon:\n"
        "  app_token: \"appTest\"\n"
        "  table_id: \"tblTest\"\n"
        "score:\n"
        "  enabled: true\n"
        f"  provider: {provider}\n"
        f"  prompt_file: {prompt_file}\n"
        "  batch_size: 100\n"
        "  providers:\n"
        f"    {provider}:\n"
        f"      api_key: \"{key}\"\n",
        encoding="utf-8",
    )
    return path


def test_load_template_missing_raises() -> None:
    with pytest.raises(RuntimeError, match="不存在"):
        score_llm.load_template("prompts/nope.md")


def test_load_template_empty_raises(tmp_path) -> None:
    empty = tmp_path / "empty.md"
    empty.write_text("   \n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="为空"):
        score_llm.load_template(str(empty))


def test_load_template_reads_repo_prompt() -> None:
    text = score_llm.load_template("prompts/score.md")

    assert "基础提示词" in text and "0 分闸门" in text


def test_build_prompt_fields_and_empty_placeholder() -> None:
    batch = [{"话题名称": "话题A", "资讯链接": ["https://a/1"]}]

    prompt = score_llm.build_prompt("模板", batch, [])

    assert "## 本批话题（含横向上文）" in prompt
    assert "1. 话题名称：话题A" in prompt
    assert "可使用工具：（空）" in prompt
    assert "资讯链接：https://a/1" in prompt
    assert "相关AI原理：（空）" in prompt
    assert "出处来源：（空）" in prompt


def test_build_prompt_truncates_principle_field() -> None:
    long_text = "原" * 350
    prompt = score_llm.build_prompt("模板", [{"话题名称": "A", "相关AI原理": long_text}], [])

    line = next(ln for ln in prompt.splitlines() if "相关AI原理" in ln)
    value = line.split("：", 1)[1]
    assert len(value) == 301 and value.endswith("…")


def test_build_prompt_prior_scores_format() -> None:
    prompt = score_llm.build_prompt("模板", [{"话题名称": "A"}], [("话题A", "3.5"), ("话题B", "1.0")])

    assert "### 已打分参考（供横向对比，不要重复打分）" in prompt
    assert "- 话题A：3.5" in prompt and "- 话题B：1.0" in prompt


def test_build_prompt_prior_scores_capped_200() -> None:
    prior = [(f"话题{i}", str(i)) for i in range(250)]

    prompt = score_llm.build_prompt("模板", [{"话题名称": "A"}], prior)

    refs = [ln for ln in prompt.splitlines() if ln.startswith("- 话题")]
    assert len(refs) == 200
    assert "- 话题249：249" in refs and all("话题0" not in r for r in refs)


def test_resolve_for_score_env_fallback(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env-ds")
    conf = ScoreConf(provider="deepseek", providers={})

    resolved = score_llm.resolve_for_score(conf, None)

    assert resolved.api_key == "sk-env-ds"
    assert resolved.base_url == "https://api.deepseek.com/v1"
    assert resolved.tool_label == "DS"


def test_resolve_for_score_config_override() -> None:
    conf = ScoreConf(provider="minimax", providers={"minimax": ProviderConf(api_key="sk-x", model="M-custom")})

    resolved = score_llm.resolve_for_score(conf, "minimax")

    assert resolved.api_key == "sk-x" and resolved.model == "M-custom"
    assert resolved.tool_label == "MMax"


def test_resolve_for_score_missing_key_raises() -> None:
    with pytest.raises(RuntimeError, match="缺少 api_key"):
        score_llm.resolve_for_score(ScoreConf(provider="minimax", providers={}), None)


def test_call_llm_delegates_to_post_chat(monkeypatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(extract_llm, "post_chat", lambda conf, prompt, timeout=180.0: seen.append(prompt) or "raw")

    assert score_llm.call_llm(ProviderConf(api_key="k"), "提问") == "raw"
    assert seen == ["提问"]


def test_read_rows_extra_fields_unified_keys(monkeypatch) -> None:
    calls: list[list[str]] = []
    _patch_lark(monkeypatch, pages=[_records(1, score="3.5")], calls=calls)

    rows = score_source.read_rows("appTest", "tblTest", fields=(*score_source.SCORE_FIELDS, "MMax打分", "MMax理由"))

    row = rows[0]
    assert row["打分"] == "3.5" and row["理由"] == "原因"
    assert "MMax打分" not in row and "MMax理由" not in row
    assert set(row) == {"record_id", *score_source.SCORE_FIELDS, "打分", "理由"}
    arg = next(c for c in calls if "+record-list" in c)
    assert "MMax打分" in arg and "MMax理由" in arg


def test_read_rows_default_fields_have_no_score_keys(monkeypatch) -> None:
    _patch_lark(monkeypatch, pages=[_records(1, score="3.5")])

    rows = score_source.read_rows("appTest", "tblTest")

    assert set(rows[0]) == {"record_id", *score_source.SCORE_FIELDS}


def test_has_score_only_checks_score_column() -> None:
    assert score_source._has_score({"打分": "3.5", "理由": ""}) is True
    assert score_source._has_score({"打分": ["3.5"], "理由": ""}) is True
    assert score_source._has_score({"打分": "", "理由": "旧理由"}) is False
    assert score_source._has_score({"打分": None, "理由": "旧理由"}) is False
    assert score_source._has_score({}) is False


def test_plan_pending_only_score_column_skips_and_force() -> None:
    rows = [
        {"话题名称": "A", "打分": "3.5", "理由": "x"},
        {"话题名称": "B", "打分": "", "理由": "旧理由"},
        {"话题名称": "C", "打分": None, "理由": None},
        {"话题名称": "D"},
        {"话题名称": "E", "打分": ["3.5"], "理由": "x"},
    ]

    pending, skipped = score_source.plan_pending(rows, "minimax")
    assert [r["话题名称"] for r in pending] == ["B", "C", "D"]
    assert [r["话题名称"] for r in skipped] == ["A", "E"]

    all_pending, none_skipped = score_source.plan_pending(rows, "minimax", force=True)
    assert len(all_pending) == 5 and none_skipped == []


def test_read_rows_limit_then_group(monkeypatch) -> None:
    _patch_lark(monkeypatch, pages=[_records(120)])

    rows = score_source.read_rows("appTest", "tblTest", limit=101)
    batches = score_source.group_batches(rows)

    assert len(rows) == 101 and [len(b) for b in batches] == [100, 1]


def test_run_prompt_missing_rc2_no_lark(tmp_path, monkeypatch, caplog) -> None:
    cfg = _write_cfg(tmp_path, prompt_file="prompts/nope.md")
    calls: list[list[str]] = []
    _patch_lark(monkeypatch, pages=[_records(1)], field_names=_MM_FIELDS, calls=calls)

    with caplog.at_level(logging.ERROR):
        rc = score_flow.main(["--config", str(cfg), "--db", str(tmp_path / "t.sqlite3")])

    assert rc == 2 and calls == [] and "提示词" in caplog.text


def test_run_missing_key_rc2_no_calls(tmp_path, monkeypatch, caplog) -> None:
    cfg = _write_cfg(tmp_path, key="")
    calls: list[list[str]] = []
    posts: list[str] = []
    _patch_lark(monkeypatch, pages=[_records(1)], field_names=_MM_FIELDS, calls=calls)
    monkeypatch.setattr(extract_llm.httpx, "post", lambda *a, **k: posts.append("x"))

    with caplog.at_level(logging.ERROR):
        rc = score_flow.main(["--config", str(cfg), "--db", str(tmp_path / "t.sqlite3")])

    assert rc == 2 and calls == [] and posts == [] and "api_key" in caplog.text


def test_run_apply_writes(tmp_path, monkeypatch) -> None:
    cfg = _write_cfg(tmp_path)
    calls: list[list[str]] = []
    _patch_lark(monkeypatch, pages=[_records(1)], field_names=_MM_FIELDS, calls=calls)
    _stub_llm_echo(monkeypatch)

    rc = score_flow.main(["--apply", "--config", str(cfg), "--db", str(tmp_path / "t.sqlite3")])

    assert rc == 0 and any("+record-batch-update" in c for c in calls)


def test_run_dry_run_prints_template_provider_sizes(tmp_path, monkeypatch, caplog) -> None:
    cfg = _write_cfg(tmp_path)
    _patch_lark(monkeypatch, pages=[_records(3)], field_names=_MM_FIELDS)
    _stub_llm_echo(monkeypatch)

    with caplog.at_level(logging.INFO):
        rc = score_flow.main(["--config", str(cfg), "--db", str(tmp_path / "t.sqlite3")])

    assert rc == 0
    assert "模板=prompts/score.md" in caplog.text
    assert "provider=minimax（MMax）" in caplog.text
    assert "每批行数=[3]" in caplog.text and "横向上文=0" in caplog.text


def test_run_existing_scores_skipped_as_context(tmp_path, monkeypatch, caplog) -> None:
    cfg = _write_cfg(tmp_path)
    page = _records(2, score="3.5") + _records(1, start=3)
    _patch_lark(monkeypatch, pages=[page], field_names=_MM_FIELDS)
    _stub_llm_echo(monkeypatch)

    with caplog.at_level(logging.INFO):
        rc = score_flow.main(["--config", str(cfg), "--db", str(tmp_path / "t.sqlite3")])

    assert rc == 0
    assert "待打分=1" in caplog.text and "跳过=2" in caplog.text and "横向上文=2" in caplog.text
