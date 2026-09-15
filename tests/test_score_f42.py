"""F42：文档一致性（CLI/OPS/AGENTS/DESIGN）+ 闸门/否决写入形态 + 多批分布校验（全离线）。"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from feedkicker import bitable_lark, score_llm, score_write
from feedkicker.config_models import PROJECT_ROOT, ProviderConf

_DIMS = {
    "普适痛点强度": 4.0, "分层承载力": 3.5, "可演示性": 5.0,
    "时效与稀缺": 4.0, "内容复用价值": 3.5, "讲解成本": 2.0,
}


class FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _doc(name: str) -> str:
    return (PROJECT_ROOT / name).read_text(encoding="utf-8")


def _item(name: str, *, total: object, scores: dict, reason: str, **over: object) -> dict:
    item: dict[str, object] = {
        "record_id": f"rec{name}", "话题名称": name, "weighted_total": total,
        "scores": scores, "reason": reason, "risk_flag": False, "source_flag": False,
    }
    item.update(over)
    return item


def _json_from_args(args: list[str]) -> dict:
    raw = args[args.index("--json") + 1]
    if raw.startswith("@"):
        return json.loads(Path(raw[1:]).read_text(encoding="utf-8"))
    return json.loads(raw)


def _stub_lark(monkeypatch: pytest.MonkeyPatch, payloads: list[dict]) -> None:
    def fake_run(args, stdin_text=None, timeout=120):
        if args[:2] == ["base", "--help"]:
            return FakeProc(0, "+record-batch-update")
        if "+record-batch-update" in args:
            payloads.append(_json_from_args(args)["update_records"])
        return FakeProc(0, "{}")

    monkeypatch.setattr(bitable_lark, "_run", fake_run)


def test_cli_md_documents_tc_score() -> None:
    text = _doc("docs/CLI.md")

    assert "9 命令" in text and "## tc-score" in text
    section = text.split("## tc-score", 1)[1].split("\n## ", 1)[0]
    for flag in ("--apply", "--dry-run", "--provider", "--limit", "--max-calls", "--force", "--env", "--config", "--db"):
        assert flag in section
    for token in ("MMax打分", "MMax理由", "DS打分", "DS理由", "退出码", "failed_writes", "written"):
        assert token in section


def test_cli_md_exit_codes_and_idempotency() -> None:
    section = _doc("docs/CLI.md").split("## tc-score", 1)[1].split("\n## ", 1)[0]

    assert "全部写入失败" in section and "只补空" in section and "--force" in section
    assert "绝不触碰其它列" in section


def test_cli_md_help_flags_match_actual(capsys) -> None:
    from feedkicker import score_flow

    with pytest.raises(SystemExit):
        score_flow.main(["--help"])
    out = capsys.readouterr().out
    section = _doc("docs/CLI.md").split("## tc-score", 1)[1].split("\n## ", 1)[0]

    for flag in ("--apply", "--dry-run", "--provider", "--limit", "--max-calls", "--force"):
        assert flag in out and flag in section


def test_ops_md_documents_score_section() -> None:
    text = _doc("docs/OPS.md")

    assert "score` 段补充" in text
    assert "prompts/score.md" in text
    assert "MiniMax_Key" in text and "DEEPSEEK_API_KEY" in text
    assert "batch_size=20" in text and "timeout_seconds=600" in text
    assert "只补空" in text


def test_design_reason_overlong_truncates_not_drops() -> None:
    section = _doc("docs/DESIGN.md").split("### 26.4 提示词与 JSON 契约", 1)[1].split("### 26.5", 1)[0]

    assert "截断" in section and "WARNING" in section and "不丢弃" in section


def test_design_write_verb_matches_cli_md() -> None:
    design = _doc("docs/DESIGN.md")
    cli = _doc("docs/CLI.md")

    assert "base +record-batch-update" in design
    assert "+record-batch-update" in cli.split("## tc-score", 1)[1].split("\n## ", 1)[0]
    assert "base +record-update" not in design


def test_design_module_tree_and_table_match_score_files() -> None:
    design = _doc("docs/DESIGN.md")
    files = sorted(p.name for p in (PROJECT_ROOT / "feedkicker").glob("score_*.py"))

    assert files == [
        "score_config.py", "score_flow.py", "score_llm.py", "score_parse.py",
        "score_report.py", "score_source.py", "score_write.py",
    ]
    for name in files:
        assert name in design, name
    assert "| `score_config.py` |" in design


def test_design_checklist_all_checked() -> None:
    section = _doc("docs/DESIGN.md").split("### 26.11 清单", 1)[1]

    for feature in ("F38", "F39", "F40", "F41", "F42"):
        assert f"- [x] {feature} " in section, feature
    assert "- [ ]" not in section


def test_design_records_aligned_differences() -> None:
    section = _doc("docs/DESIGN.md").split("### 26.12 实现说明", 1)[1]

    assert "`scores`" in section and "`dimensions`" in section
    assert 'gate: "pass"|"zero"' in section
    assert "宽容回退" in section
    assert "JSON schema" in section
    assert "仅看 `打分` 列非空" in section


def test_agents_md_has_tc_score() -> None:
    text = _doc("AGENTS.md")

    assert "tc-score" in text and "--apply" in text


def _collected_count() -> int:
    """子进程跑 `pytest --collect-only -q`（只收集、不执行，无网络）解析实际用例数。"""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    match = re.search(r"(\d+) tests? collected", proc.stdout)
    if match is None:
        pytest.fail(f"无法解析 pytest 收集数: {proc.stdout[-300:]} / {proc.stderr[-300:]}")
    return int(match.group(1))


def test_agents_md_use_count_matches() -> None:
    count = _collected_count()

    assert count > 0
    assert f"{count} 用例" in _doc("AGENTS.md")


def test_agents_md_count_is_not_stale() -> None:
    assert "685 用例" not in _doc("AGENTS.md")


def test_gate_zero_write_form() -> None:
    item = _item("A", total=0.0, scores={}, reason="无内容内核；抢救建议：改造为「财报里企业如何裁 AI 预算」")
    cell = score_write._cell(item, "MMax")

    assert cell["MMax打分"] == "0.0"
    assert "无内容内核" in cell["MMax理由"] and "抢救建议" in cell["MMax理由"]


def test_veto_write_form_keeps_dimensions() -> None:
    scores = dict(_DIMS)
    scores["普适痛点强度"] = 0
    item = _item("A", total=0.0, scores=scores, reason="普适痛点强度=0，致命否决")
    cell = score_write._cell(item, "MMax")

    assert cell["MMax打分"] == "0.0"
    assert "普适痛点0.0/分层承载3.5/可演示5.0/时效稀缺4.0/复用价值3.5/讲解成本2.0" in cell["MMax理由"]


def test_write_payload_only_two_columns_for_gate_and_veto(monkeypatch) -> None:
    payloads: list[dict] = []
    _stub_lark(monkeypatch, payloads)
    rows = [
        _item("A", total=0.0, scores={}, reason="无内容内核"),
        _item("B", total=0.0, scores={**_DIMS, "分层承载力": 0}, reason="否决"),
    ]

    stats = score_write.write_scores(score_write.WriteConf("appTest", "tblTest", "minimax"), rows, dry_run=False)

    assert stats.written == 2
    assert set(payloads[0]["recA"]) == {"MMax打分", "MMax理由"}
    assert set(payloads[0]["recB"]) == {"MMax打分", "MMax理由"}


def test_multi_batch_distribution_violations(monkeypatch) -> None:
    import re

    def fake_call(conf, prompt, timeout=180.0):
        names = [n.strip() for n in re.findall(r"^\d+\. 话题名称：(.+)$", prompt, re.MULTILINE)]
        scores = [
            {"话题名称": n, "gate": "pass", "dimensions": {k: 5.0 for k in _DIMS}, "reason": "依据"}
            for n in names
        ]
        return json.dumps({"scores": scores}, ensure_ascii=False)

    monkeypatch.setattr(score_llm, "call_llm", fake_call)
    batches = [[{"话题名称": "话题1"}], [{"话题名称": "话题2"}]]

    result = score_llm.refine_batches(ProviderConf(api_key="k"), "模板", batches, [], 0)

    assert len(result.scored) == 2
    assert len(result.violations) >= 2
    assert any("≥4.0" in v for v in result.violations)


def test_design_config_field_count_is_14() -> None:
    design = _doc("docs/DESIGN.md")

    assert "共 14 个顶层字段" in design and "共 13 个顶层字段" not in design
    assert "score: ScoreConf{" in design


def test_no_stale_eight_command_count() -> None:
    for name in ("docs/CLI.md", "docs/PRD.md", "docs/DESIGN.md", "README.md", "AGENTS.md"):
        assert "8 命令" not in _doc(name), name


def test_examples_have_score_section_with_placeholders() -> None:
    for name in ("config-dev.yaml.example", "config-test.yaml.example", "config-prod.yaml.example"):
        data = yaml.safe_load(_doc(name))
        sc = data["score"]
        assert sc["batch_size"] == 20 and sc["timeout_seconds"] == 600
        assert sc["providers"]["minimax"]["api_key"].startswith("<")


def test_prd_and_cli_force_and_limit_alignment() -> None:
    prd = _doc("docs/PRD.md")
    section = _doc("docs/CLI.md").split("## tc-score", 1)[1].split("\n## ", 1)[0]

    assert "[--force]" in prd and "| `--force` |" in prd
    assert "| `--limit` |" in section and "无对应配置项" in section


def test_cli_md_batch_numbers_match_group_batches() -> None:
    from feedkicker import score_source

    sizes = [len(b) for b in score_source.group_batches([{} for _ in range(85)], 20)]
    section = _doc("docs/CLI.md").split("## tc-score", 1)[1].split("\n## ", 1)[0]

    assert sizes == [20, 20, 20, 20, 5]
    assert "批数=5" in section and "[20, 20, 20, 20, 5]" in section and '"batches": 5' in section
    assert "85 行按默认批大小 20 = 5 批" in _doc("docs/PRD.md")


def test_design_module_table_matches_implementation() -> None:
    section = _doc("docs/DESIGN.md").split("### 26.2 模块划分", 1)[1].split("### 26.3", 1)[0]

    assert "ensure_columns(app_token, table_id, provider) -> int" in section
    assert "call_llm(conf, prompt, timeout=600.0) -> str" in section
    assert "normalize(items, rows) -> (list[dict], DistCheck)" in section
    assert "`ensure_columns(...)`" not in section
