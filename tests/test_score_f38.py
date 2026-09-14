"""F38：tc-score CLI 骨架 / score 配置段 / 目标列校验 / 退出码（全离线，mock lark-cli）。"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest

from feedkicker import bitable_lark, score_flow, score_source
from feedkicker.config import load_config

_MM_FIELDS = ["话题名称", "可使用工具", "相关AI原理", "资讯链接", "出处来源", "MMax打分", "MMax理由"]
_DS_FIELDS = ["话题名称", "可使用工具", "相关AI原理", "资讯链接", "出处来源", "DS打分", "DS理由"]


def _stub_llm_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    from feedkicker import score_llm

    def fake(conf, prompt):
        names = re.findall(r"^\d+\. 话题名称：(.+)$", prompt, re.MULTILINE)
        dims = {
            "普适痛点强度": 4.0, "分层承载力": 4.0, "可演示性": 4.0,
            "时效与稀缺": 4.0, "内容复用价值": 4.0, "讲解成本": 4.0,
        }
        results = [
            {
                "话题名称": n.strip(), "gate": "pass", "scores": dims, "weighted_total": 4.0,
                "risk_flag": False, "source_flag": False, "reason": "依据字段",
            }
            for n in names
        ]
        return json.dumps({"results": results}, ensure_ascii=False)

    monkeypatch.setattr(score_llm, "call_llm", fake)


class FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _records(n: int, start: int = 1) -> list[dict[str, object]]:
    return [
        {
            "record_id": f"rec{start + i}",
            "fields": {
                "话题名称": f"话题{start + i}",
                "可使用工具": "工具X",
                "相关AI原理": "原理Y",
                "资讯链接": "https://a/1",
                "出处来源": "量子位",
            },
        }
        for i in range(n)
    ]


def _patch_lark(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pages: list[list[dict[str, object]]] | None = None,
    field_names: list[str] | None = None,
    field_data: dict[str, object] | None = None,
    data_override: dict[str, object] | None = None,
    calls: list[list[str]] | None = None,
) -> None:
    def fake_run(args, stdin_text=None, timeout=120):
        if calls is not None:
            calls.append(list(args))
        if data_override is not None:
            return FakeProc(0, json.dumps(data_override, ensure_ascii=False))
        if "+field-list" in args:
            payload = field_data or {"fields": [{"field_name": n} for n in (field_names or [])]}
            return FakeProc(0, json.dumps({"data": payload}, ensure_ascii=False))
        if "+record-list" in args:
            offset = int(args[args.index("--offset") + 1])
            idx = offset // bitable_lark._CHUNK
            page = pages[idx] if pages and idx < len(pages) else []
            return FakeProc(0, json.dumps({"data": {"records": page}}, ensure_ascii=False))
        return FakeProc(0, "{}")

    monkeypatch.setattr(bitable_lark, "_run", fake_run)


def _write_cfg(tmp_path: Path, *, provider: str = "minimax", batch_size: int = 100) -> Path:
    path = tmp_path / "score-cfg.yaml"
    path.write_text(
        "salon:\n"
        "  app_token: \"appTest\"\n"
        "  table_id: \"tblTest\"\n"
        "score:\n"
        "  enabled: true\n"
        f"  provider: {provider}\n"
        "  prompt_file: prompts/score.md\n"
        f"  batch_size: {batch_size}\n"
        "  max_calls: 0\n"
        "  providers:\n"
        f"    {provider}:\n"
        "      api_key: \"sk-test\"\n",
        encoding="utf-8",
    )
    return path


def _args(cfg: Path, tmp_path: Path, *extra: str) -> list[str]:
    return [*extra, "--config", str(cfg), "--db", str(tmp_path / "t.sqlite3")]


def test_main_help_rc0(capsys) -> None:
    with pytest.raises(SystemExit) as ei:
        score_flow.main(["--help"])

    assert ei.value.code == 0
    out = capsys.readouterr().out
    for flag in ("--apply", "--dry-run", "--provider", "--limit", "--max-calls", "--force", "--env", "--config", "--db"):
        assert flag in out


def test_default_dry_run_rc0_prints_plan(tmp_path, monkeypatch, capsys, caplog) -> None:
    cfg = _write_cfg(tmp_path)
    _patch_lark(monkeypatch, pages=[_records(3)], field_names=_MM_FIELDS)
    _stub_llm_echo(monkeypatch)

    with caplog.at_level(logging.INFO):
        rc = score_flow.main(_args(cfg, tmp_path))

    out = capsys.readouterr().out
    assert rc == 0
    assert "总行数=3" in out and "批数=1" in out and "待打分=3" in out
    assert "总行数=3" in caplog.text and "批数=1" in caplog.text


def test_apply_rejected_rc2_no_lark(tmp_path, monkeypatch, caplog) -> None:
    cfg = _write_cfg(tmp_path)
    calls: list[list[str]] = []
    _patch_lark(monkeypatch, pages=[_records(3)], field_names=_MM_FIELDS, calls=calls)

    with caplog.at_level(logging.ERROR):
        rc = score_flow.main(_args(cfg, tmp_path, "--apply"))

    assert rc == 2 and "尚未实现" in caplog.text and calls == []


@pytest.mark.parametrize(
    ("provider", "field_names"),
    [
        ("minimax", ["话题名称", "可使用工具", "相关AI原理", "资讯链接", "出处来源", "MMax理由"]),
        ("deepseek", ["话题名称", "可使用工具", "相关AI原理", "资讯链接", "出处来源", "DS打分"]),
    ],
)
def test_missing_target_column_rc2(tmp_path, monkeypatch, caplog, provider, field_names) -> None:
    cfg = _write_cfg(tmp_path, provider=provider)
    _patch_lark(monkeypatch, pages=[_records(2)], field_names=field_names)

    with caplog.at_level(logging.ERROR):
        rc = score_flow.main(_args(cfg, tmp_path, "--dry-run"))

    assert rc == 2 and "目标列缺失" in caplog.text


def test_columns_present_rc0_after_read(tmp_path, monkeypatch) -> None:
    cfg = _write_cfg(tmp_path, provider="deepseek")
    _patch_lark(monkeypatch, pages=[_records(1)], field_names=_DS_FIELDS)
    _stub_llm_echo(monkeypatch)

    assert score_flow.main(_args(cfg, tmp_path)) == 0


def test_batch_size_overflow_rc2_no_lark(tmp_path, monkeypatch, caplog) -> None:
    cfg = _write_cfg(tmp_path, batch_size=101)
    calls: list[list[str]] = []
    _patch_lark(monkeypatch, pages=[_records(1)], field_names=_MM_FIELDS, calls=calls)

    with caplog.at_level(logging.ERROR):
        rc = score_flow.main(_args(cfg, tmp_path))

    assert rc == 2 and calls == [] and "score.batch_size" in caplog.text


def test_score_unknown_keys_warn_and_still_load(tmp_path, caplog) -> None:
    path = tmp_path / "unknown.yaml"
    path.write_text(
        "score:\n"
        "  enabledd: true\n"
        "  providers:\n"
        "    minimax:\n"
        "      modell: x\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        cfg = load_config(path, app_env="test")

    assert "score.enabledd" in caplog.text
    assert "score.providers.minimax.modell" in caplog.text
    assert cfg.score.enabled is False


def test_score_conf_defaults() -> None:
    cfg = load_config(app_env="test")

    assert cfg.score.enabled is False
    assert cfg.score.prompt_file == "prompts/score.md"
    assert cfg.score.batch_size == 100
    assert cfg.score.provider == "minimax"
    assert cfg.score.max_calls == 0
    assert cfg.score.providers == {}


def test_load_score_section_and_placeholder_cleared(tmp_path) -> None:
    path = tmp_path / "score.yaml"
    path.write_text(
        "score:\n"
        "  enabled: true\n"
        "  provider: deepseek\n"
        "  batch_size: 5\n"
        "  max_calls: 2\n"
        "  providers:\n"
        "    deepseek:\n"
        "      api_key: \"<deepseek-key>\"\n"
        "      model: \"deepseek-reasoner\"\n",
        encoding="utf-8",
    )

    cfg = load_config(path, app_env="test")

    assert cfg.score.enabled is True
    assert cfg.score.provider == "deepseek" and cfg.score.batch_size == 5 and cfg.score.max_calls == 2
    assert cfg.score.providers["deepseek"].api_key == ""
    assert cfg.score.providers["deepseek"].model == "deepseek-reasoner"


def test_group_batches_empty() -> None:
    assert score_source.group_batches([]) == []


@pytest.mark.parametrize(("n", "expected"), [(1, 1), (100, 1), (101, 2)])
def test_group_batches_sizes(n, expected) -> None:
    rows = [{"record_id": f"rec{i}"} for i in range(n)]

    batches = score_source.group_batches(rows)

    assert len(batches) == expected
    assert all(len(b) <= score_source.MAX_SCORE_BATCH for b in batches)


def test_group_batches_clamps_oversized_size() -> None:
    rows = [{"record_id": f"rec{i}"} for i in range(150)]

    batches = score_source.group_batches(rows, size=1000)

    assert len(batches) == 2
    assert all(len(b) <= score_source.MAX_SCORE_BATCH for b in batches)


def test_read_rows_paginates_all(monkeypatch) -> None:
    _patch_lark(monkeypatch, pages=[_records(bitable_lark._CHUNK, start=1), _records(1, start=201)])

    rows = score_source.read_rows("appTest", "tblTest")

    assert len(rows) == bitable_lark._CHUNK + 1
    assert rows[0]["record_id"] == "rec1"
    assert rows[-1]["record_id"] == "rec201"


def test_read_rows_limit_truncates(monkeypatch) -> None:
    _patch_lark(monkeypatch, pages=[_records(5)])

    rows = score_source.read_rows("appTest", "tblTest", limit=3)

    assert len(rows) == 3


def test_read_rows_unrecognizable_response_raises(monkeypatch) -> None:
    _patch_lark(monkeypatch, data_override={"data": {}})

    with pytest.raises(RuntimeError):
        score_source.read_rows("appTest", "tblTest")


def test_read_rows_keeps_only_score_fields_and_record_id(monkeypatch) -> None:
    page = [
        {
            "record_id": "recX",
            "fields": {
                "话题名称": "T",
                "可使用工具": "U",
                "相关AI原理": "P",
                "资讯链接": "L",
                "出处来源": "S",
                "讨论状态": ["已选题"],
            },
        }
    ]
    _patch_lark(monkeypatch, pages=[page])

    rows = score_source.read_rows("appTest", "tblTest")

    assert set(rows[0]) == {"record_id", *score_source.SCORE_FIELDS}


def test_entrypoint_silences_httpx_logger(tmp_path, monkeypatch) -> None:
    cfg = _write_cfg(tmp_path)
    _patch_lark(monkeypatch, pages=[_records(1)], field_names=_MM_FIELDS)
    _stub_llm_echo(monkeypatch)

    assert score_flow.main(_args(cfg, tmp_path)) == 0
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
