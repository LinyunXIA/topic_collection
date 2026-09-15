"""F40：契约解析 / 闸门与否决 / 缺失归一 / 分布校验 / 重试计数 / dry-run 报告（全离线）。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from feedkicker import bitable_lark, score_flow, score_llm, score_parse, score_report

_MM_FIELDS = ["话题名称", "可使用工具", "相关AI原理", "资讯链接", "出处来源", "MMax打分", "MMax理由"]

_DIMS_4 = {
    "普适痛点强度": 4.0, "分层承载力": 4.0, "可演示性": 4.0,
    "时效与稀缺": 4.0, "内容复用价值": 4.0, "讲解成本": 4.0,
}


class FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _records(n: int, *, start: int = 1) -> list[dict[str, object]]:
    return [
        {
            "record_id": f"rec{start + i}",
            "fields": {"话题名称": f"话题{start + i}", "可使用工具": "工具X"},
        }
        for i in range(n)
    ]


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


def _write_cfg(tmp_path: Path, *, batch_size: int = 100) -> Path:
    path = tmp_path / "f40-cfg.yaml"
    path.write_text(
        "salon:\n"
        "  app_token: \"appTest\"\n"
        "  table_id: \"tblTest\"\n"
        "score:\n"
        "  enabled: true\n"
        "  provider: minimax\n"
        "  prompt_file: prompts/score.md\n"
        f"  batch_size: {batch_size}\n"
        "  providers:\n"
        "    minimax:\n"
        "      api_key: \"sk-test\"\n",
        encoding="utf-8",
    )
    return path


def _args(cfg: Path, tmp_path: Path, *extra: str) -> list[str]:
    return [*extra, "--config", str(cfg), "--db", str(tmp_path / "t.sqlite3")]


def _summary(out: str) -> dict:
    return json.loads([ln for ln in out.splitlines() if ln.startswith("{")][-1])


def _result(name: str, **over: object) -> dict[str, object]:
    item: dict[str, object] = {
        "话题名称": name, "gate": "pass", "dimensions": dict(_DIMS_4), "missing": [],
        "weighted_total": 4.0, "risk_flag": False, "source_flag": False, "reason": "依据 可使用工具",
    }
    item.update(over)
    return item


def _results(names: list[str], **over: object) -> str:
    return json.dumps({"scores": [_result(n, **over) for n in names]}, ensure_ascii=False)


def _stub_llm(monkeypatch: pytest.MonkeyPatch, responder) -> list[str]:
    prompts: list[str] = []

    def fake(conf, prompt, timeout=180.0):
        prompts.append(prompt)
        return responder(prompt)

    monkeypatch.setattr(score_llm, "call_llm", fake)
    return prompts


def _echo_responder(prompt: str) -> str:
    import re

    names = [m.strip() for m in re.findall(r"^\d+\. 话题名称：(.+)$", prompt, re.MULTILINE)]
    return _results(names)


def test_parse_scores_plain_and_fence_and_thinking() -> None:
    valid = {"话题名称": "X", "gate": "pass", "dimensions": dict(_DIMS_4), "reason": "依据"}
    assert score_parse.parse_scores(_results(["A"]))[0]["话题名称"] == "A"
    fenced = "说明\n```json\n" + json.dumps({"scores": [{**valid, "话题名称": "B"}]}, ensure_ascii=False) + "\n```\n"
    assert score_parse.parse_scores(fenced)[0]["话题名称"] == "B"
    thinking = '<think foo="1">推导</think>' + json.dumps({"scores": [{**valid, "话题名称": "C"}]}, ensure_ascii=False)
    assert score_parse.parse_scores(thinking)[0]["话题名称"] == "C"


def test_parse_scores_results_fallback_compat() -> None:
    raw = json.dumps(
        {
            "results": [
                {"话题名称": "D", "gate": "pass", "scores": dict(_DIMS_4), "reason": "依据"}
            ]
        },
        ensure_ascii=False,
    )

    items, _ = score_parse.normalize(
        score_parse.parse_scores(raw), [{"record_id": "recD", "话题名称": "D"}]
    )

    assert items[0]["话题名称"] == "D" and items[0]["weighted_total"] == 4.0


def test_dimensions_key_drives_weighted_total() -> None:
    raw = _results(["E"])
    items, _ = score_parse.normalize(
        score_parse.parse_scores(raw), [{"record_id": "recE", "话题名称": "E"}]
    )

    assert items[0]["scores"] == _DIMS_4 and items[0]["weighted_total"] == 4.0


@pytest.mark.parametrize("raw", ['{}', '{"results": "x"}', '{"results": 3}', '{"scores": "x"}'])
def test_parse_scores_invalid_raises(raw: str) -> None:
    with pytest.raises(ValueError):
        score_parse.parse_scores(raw)


def test_parse_results_drops_invalid_items() -> None:
    raw = json.dumps(
        {
            "scores": [
                "not-a-dict",
                {"gate": "pass"},
                {"话题名称": "  "},
                {"话题名称": "好话题", "gate": "pass", "dimensions": dict(_DIMS_4), "reason": "依据"},
            ]
        },
        ensure_ascii=False,
    )

    items, dropped = score_parse.parse_results(raw)

    assert [i["话题名称"] for i in items] == ["好话题"]
    assert dropped == 3


@pytest.mark.parametrize(
    "dimensions",
    [
        {**_DIMS_4, "普适痛点强度": 6},
        {**_DIMS_4, "普适痛点强度": -1},
        {**_DIMS_4, "普适痛点强度": "abc"},
        {**_DIMS_4, "普适痛点强度": True},
        {**_DIMS_4, "讲解成本": None},
    ],
)
def test_parse_drops_invalid_dimension(dimensions) -> None:
    items, dropped = score_parse.parse_results(_results(["A"], dimensions=dimensions))

    assert items == [] and dropped == 1
    normalized, _ = score_parse.normalize(items, [{"record_id": "recA", "话题名称": "A"}])
    assert normalized == []


def test_parse_drops_missing_reason() -> None:
    items, dropped = score_parse.parse_results(_results(["A"], reason="   "))

    assert items == [] and dropped == 1


def test_parse_accepts_half_step_and_missing_literal() -> None:
    dimensions = {**_DIMS_4, "普适痛点强度": 0.5, "分层承载力": "缺失"}

    items, dropped = score_parse.parse_results(_results(["A"], dimensions=dimensions))

    assert len(items) == 1 and dropped == 0


def test_parse_gate_zero_needs_only_reason() -> None:
    items, dropped = score_parse.parse_results(
        _results(["A"], gate="zero", dimensions={}, reason="无内容内核")
    )

    assert len(items) == 1 and dropped == 0
    normalized, _ = score_parse.normalize(items, [{"record_id": "recA", "话题名称": "A"}])
    assert normalized[0]["weighted_total"] == 0.0


def test_weighted_total_with_all_dims() -> None:
    total, missing = score_parse.weighted_total(dict(_DIMS_4))

    assert total == 4.0 and missing == []


def test_weighted_total_rounds_to_one_decimal() -> None:
    scores = dict(_DIMS_4)
    scores["普适痛点强度"] = 3.5

    total, _ = score_parse.weighted_total(scores)

    assert total == 3.9


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ((5, 5, 2.5, 0, 0.5, 2.5), 3.3),
        ((5, 5, 2.5, 5, 4.5, 2.5), 4.3),
        ((2.5, 2.5, 0, 0, 0, 0), 1.3),
    ],
)
def test_weighted_total_half_up_ties(values: tuple[float, ...], expected: float) -> None:
    """R11-14：x.25 四舍五入（3.25→3.3/4.25→4.3/1.25→1.3），银行家舍入会得偶数尾。"""
    scores = dict(zip(_DIMS_4, values, strict=True))

    total, missing = score_parse.weighted_total(scores)

    assert total == expected and missing == []


def test_weighted_total_missing_denominator_396_over_92_stays_4_3() -> None:
    """R11-14 回归：缺失维剔除后 396/92=4.304… 非 tie，改 HALF_UP 后仍为 4.3。"""
    scores = dict(_DIMS_4)
    scores["普适痛点强度"] = 5
    scores["讲解成本"] = "缺失"

    total, missing = score_parse.weighted_total(scores)

    assert total == 4.3 and missing == ["讲解成本"]


def test_gate_zero_string_skips_dimensions() -> None:
    items, _ = score_parse.normalize(
        [_result("A", gate="zero", dimensions={}, reason="无内容内核")],
        [{"record_id": "rec1", "话题名称": "A"}],
    )

    assert items[0]["weighted_total"] == 0.0 and len(items[0]["missing"]) == 6


def test_gate_numeric_zero_tolerated() -> None:
    items, _ = score_parse.normalize([_result("A", gate=0)], [{"record_id": "rec1", "话题名称": "A"}])

    assert items[0]["weighted_total"] == 0.0


def test_missing_list_drives_renormalize() -> None:
    dims = dict(_DIMS_4)
    dims["普适痛点强度"] = 5
    items, _ = score_parse.normalize(
        [_result("A", dimensions=dims, missing=["讲解成本"])],
        [{"record_id": "rec1", "话题名称": "A"}],
    )

    assert items[0]["weighted_total"] == 4.3 and "讲解成本" in items[0]["missing"]


def test_veto_pain_zero_keeps_dimensions() -> None:
    scores = dict(_DIMS_4)
    scores["普适痛点强度"] = 0
    items, _ = score_parse.normalize(
        [_result("A", dimensions=scores)], [{"record_id": "rec1", "话题名称": "A"}]
    )

    assert items[0]["weighted_total"] == 0.0
    assert items[0]["scores"]["分层承载力"] == 4.0


def test_veto_layer_zero() -> None:
    scores = dict(_DIMS_4)
    scores["分层承载力"] = 0
    items, _ = score_parse.normalize(
        [_result("A", dimensions=scores)], [{"record_id": "rec1", "话题名称": "A"}]
    )

    assert items[0]["weighted_total"] == 0.0


def test_missing_dim_renormalizes_remaining() -> None:
    scores = dict(_DIMS_4)
    scores["普适痛点强度"] = 5
    scores["讲解成本"] = "缺失"
    total, missing = score_parse.weighted_total(scores)

    assert total == 4.3 and missing == ["讲解成本"]


def test_all_missing_returns_none() -> None:
    scores = {k: "缺失" for k in _DIMS_4}

    total, missing = score_parse.weighted_total(scores)
    items, _ = score_parse.normalize([_result("A", dimensions=scores)], [{"record_id": "rec1", "话题名称": "A"}])

    assert total is None and len(missing) == 6
    assert items[0]["weighted_total"] is None


def test_model_total_deviation_warns(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        items, _ = score_parse.normalize(
            [_result("A", weighted_total=1.0)], [{"record_id": "rec1", "话题名称": "A"}]
        )

    assert items[0]["weighted_total"] == 4.0
    assert "偏差" in caplog.text


def test_reason_truncated_over_100_chars(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        items, _ = score_parse.normalize(
            [_result("A", reason="字" * 120)], [{"record_id": "rec1", "话题名称": "A"}]
        )

    assert len(items[0]["reason"]) == 100 and items[0]["reason"].endswith("…")
    assert "截断" in caplog.text


def test_flags_not_in_reason_serialized_by_cell() -> None:
    """#R10-10：risk/source 统一由 `_cell` 序列化，`_normalize_item` 不再追加裸标记。"""
    items, _ = score_parse.normalize(
        [_result("A", risk_flag=True, source_flag=True)],
        [{"record_id": "rec1", "话题名称": "A"}],
    )

    assert "｜risk" not in items[0]["reason"] and "｜source" not in items[0]["reason"]


def test_flags_not_duplicated_when_present() -> None:
    items, _ = score_parse.normalize(
        [_result("A", risk_flag=True, reason="含 risk 标记")],
        [{"record_id": "rec1", "话题名称": "A"}],
    )

    assert items[0]["reason"] == "含 risk 标记"


def test_normalize_backfills_record_id(caplog) -> None:
    items, dist, dropped = score_parse.normalize_results(
        [_result("Ａ话题")], [{"record_id": "recX", "话题名称": "A话题"}]
    )

    assert items[0]["record_id"] == "recX"
    assert dist.total == 1 and dropped == 0


def test_normalize_extra_item_dropped(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        items, _, dropped = score_parse.normalize_results(
            [_result("A"), _result("多余")], [{"record_id": "rec1", "话题名称": "A"}]
        )

    assert len(items) == 1 and dropped == 1 and "多余项" in caplog.text


def test_normalize_missing_input_row_dropped(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        items, _, dropped = score_parse.normalize_results(
            [_result("A")], [{"record_id": "rec1", "话题名称": "A"}, {"record_id": "rec2", "话题名称": "B"}]
        )

    assert len(items) == 1 and dropped == 1 and "缺返回行" in caplog.text


def test_check_distribution_ok() -> None:
    items = [{"weighted_total": 4.0}] * 2 + [{"weighted_total": 1.0}] * 2 + [{"weighted_total": 3.0}] * 6

    check = score_parse.check_distribution(items)

    assert check.total == 10 and check.ge4_ratio == 0.2 and check.lt2_ratio == 0.2
    assert check.violations == []


def test_check_distribution_violations_numbers() -> None:
    items = [{"weighted_total": 5.0} for _ in range(10)]

    check = score_parse.check_distribution(items)

    assert check.ge4_ratio == 1.0 and check.lt2_ratio == 0.0
    assert any("≥4.0 占比 100% 超过 20%" in v for v in check.violations)
    assert any("<2.0 占比 0% 少于 15%" in v for v in check.violations)


def test_print_dry_run_and_summary(capsys) -> None:
    planned = [{"话题名称": "A", "weighted_total": 3.5, "reason": "理由" * 40}]
    skipped = [{"话题名称": "B"}]

    score_report.print_dry_run(planned, skipped)
    score_report.print_summary(
        {
            "mode": "dry-run", "batches": 1, "llm_calls": 1, "rows": 2, "scored": 1, "skipped": 1,
            "failed_writes": 0, "failed_batches": 0, "dropped": 0, "empty_batches": 0,
        }
    )

    out = capsys.readouterr().out
    assert "[将写入] 1. A → 3.5 ｜ " in out
    assert "[已存在跳过] 1. B" in out
    assert "待写 1 / 跳过 1 / 分布校验" in out
    summary = json.loads([ln for ln in out.splitlines() if ln.startswith("{")][-1])
    assert summary["scored"] == 1 and summary["failed_writes"] == 0


def test_run_results_not_list_retries_then_failed_batch(tmp_path, monkeypatch, capsys) -> None:
    cfg = _write_cfg(tmp_path)
    _patch_lark(monkeypatch, pages=[_records(2)], field_names=_MM_FIELDS)
    _stub_llm(monkeypatch, lambda prompt: '{"scores": "bad"}')

    rc = score_flow.main(_args(cfg, tmp_path))

    summary = _summary(capsys.readouterr().out)
    assert rc == 0 and summary["failed_batches"] == 1 and summary["llm_calls"] == 2


def test_run_empty_results_counts_empty_batch(tmp_path, monkeypatch, capsys) -> None:
    cfg = _write_cfg(tmp_path)
    _patch_lark(monkeypatch, pages=[_records(2)], field_names=_MM_FIELDS)
    _stub_llm(monkeypatch, lambda prompt: '{"scores": []}')

    rc = score_flow.main(_args(cfg, tmp_path))

    summary = _summary(capsys.readouterr().out)
    assert rc == 0 and summary["empty_batches"] == 1 and summary["failed_batches"] == 0


def test_run_max_calls_counts_failure(tmp_path, monkeypatch, capsys, caplog) -> None:
    cfg = _write_cfg(tmp_path, batch_size=1)
    _patch_lark(monkeypatch, pages=[_records(2)], field_names=_MM_FIELDS)

    def boom(prompt):
        raise RuntimeError("网络炸了")

    _stub_llm(monkeypatch, boom)

    with caplog.at_level(logging.WARNING):
        rc = score_flow.main(_args(cfg, tmp_path, "--max-calls", "1"))

    summary = _summary(capsys.readouterr().out)
    assert rc == 0 and summary["llm_calls"] == 1 and summary["failed_batches"] == 1
    assert "达到 max_calls=1 上限" in caplog.text


def test_run_dry_run_listing_and_summary(tmp_path, monkeypatch, capsys) -> None:
    cfg = _write_cfg(tmp_path)
    _patch_lark(monkeypatch, pages=[_records(2)], field_names=_MM_FIELDS)
    _stub_llm(monkeypatch, _echo_responder)

    rc = score_flow.main(_args(cfg, tmp_path))

    out = capsys.readouterr().out
    assert rc == 0
    assert "[将写入] 1. 话题1 → 4.0" in out
    summary = _summary(out)
    assert summary["mode"] == "dry-run" and summary["scored"] == 2 and summary["failed_writes"] == 0


def test_run_distribution_violation_dry_run_stdout(tmp_path, monkeypatch, caplog, capsys) -> None:
    """R11-15：dry-run 全局结论只走 print_dry_run 末行；按批 WARNING 已降 DEBUG，caplog 不再出现。"""
    cfg = _write_cfg(tmp_path)
    _patch_lark(monkeypatch, pages=[_records(1)], field_names=_MM_FIELDS)
    _stub_llm(monkeypatch, lambda prompt: _results(["话题1"], dimensions={k: 5.0 for k in _DIMS_4}))

    with caplog.at_level(logging.WARNING):
        rc = score_flow.main(_args(cfg, tmp_path))

    out = capsys.readouterr().out
    assert rc == 0 and "分布校验 ≥4.0 占比 100% 超过 20%" in out
    assert "分布校验违规" not in caplog.text


def test_run_apply_global_distribution_violation_logged(tmp_path, monkeypatch, caplog) -> None:
    """R11-15：--apply 收尾对全部 scored 做一次全局校验，违规 WARNING 可见（dry-run 走 stdout 清单）。"""
    cfg = _write_cfg(tmp_path)
    _patch_lark(monkeypatch, pages=[_records(1)], field_names=_MM_FIELDS)
    _stub_llm(monkeypatch, lambda prompt: _results(["话题1"], dimensions={k: 5.0 for k in _DIMS_4}))

    with caplog.at_level(logging.WARNING, logger="feedkicker.score_flow"):
        rc = score_flow.main(_args(cfg, tmp_path, "--apply"))

    assert rc == 0
    assert "全局分布校验违规" in caplog.text and "≥4.0" in caplog.text


def test_run_apply_global_distribution_compliant_info(tmp_path, monkeypatch, caplog) -> None:
    """R11-15：全局合规（0% ≥4.0、20% <2.0）只在 INFO 给结论，不出 WARNING。"""
    import re

    cfg = _write_cfg(tmp_path)
    _patch_lark(monkeypatch, pages=[_records(10)], field_names=_MM_FIELDS)

    def responder(prompt: str) -> str:
        names = [m.strip() for m in re.findall(r"^\d+\. 话题名称：(.+)$", prompt, re.MULTILINE)]
        rows = [
            _result(n, dimensions={k: (1.0 if i % 5 == 0 else 3.0) for k in _DIMS_4})
            for i, n in enumerate(names)
        ]
        return json.dumps({"scores": rows}, ensure_ascii=False)

    _stub_llm(monkeypatch, responder)

    with caplog.at_level(logging.INFO, logger="feedkicker.score_flow"):
        rc = score_flow.main(_args(cfg, tmp_path, "--apply"))

    assert rc == 0
    assert "全局分布校验合规（10 行" in caplog.text
    assert "全局分布校验违规" not in caplog.text


def test_run_prior_context_feeds_next_batch(tmp_path, monkeypatch) -> None:
    cfg = _write_cfg(tmp_path, batch_size=1)
    _patch_lark(monkeypatch, pages=[_records(2)], field_names=_MM_FIELDS)
    prompts = _stub_llm(monkeypatch, _echo_responder)

    rc = score_flow.main(_args(cfg, tmp_path))

    assert rc == 0 and len(prompts) == 2
    assert "### 已打分参考（供横向对比，不要重复打分）" not in prompts[0]
    assert "### 已打分参考（供横向对比，不要重复打分）" in prompts[1]
    assert "- 话题1：4.0" in prompts[1]


def test_run_apply_writes(tmp_path, monkeypatch, capsys) -> None:
    cfg = _write_cfg(tmp_path)
    _patch_lark(monkeypatch, pages=[_records(1)], field_names=_MM_FIELDS)
    _stub_llm(monkeypatch, _echo_responder)

    rc = score_flow.main(_args(cfg, tmp_path, "--apply"))

    summary = _summary(capsys.readouterr().out)
    assert rc == 0 and summary["mode"] == "apply" and summary["written"] == 1


def test_normalize_same_name_two_rows_both_scored() -> None:
    rows = [{"record_id": "rec1", "话题名称": "同名话题"}, {"record_id": "rec2", "话题名称": "同名话题"}]

    normalized, _dist, dropped = score_parse.normalize_results(
        [_result("同名话题"), _result("同名话题")], rows
    )

    assert dropped == 0 and {n["record_id"] for n in normalized} == {"rec1", "rec2"}


def test_normalize_duplicate_return_dropped() -> None:
    rows = [{"record_id": "rec1", "话题名称": "同名话题"}]

    normalized, _dist, dropped = score_parse.normalize_results(
        [_result("同名话题"), _result("同名话题")], rows
    )

    assert len(normalized) == 1 and normalized[0]["record_id"] == "rec1" and dropped == 1


def test_normalize_record_id_mismatch_falls_back_to_name() -> None:
    """#R10-09：名匹配为主，record_id 仅在同一名内消歧；名不符的 id 降级按名（WARNING）。"""
    rows = [{"record_id": "rec1", "话题名称": "A"}, {"record_id": "rec2", "话题名称": "B"}]

    normalized, _dist, dropped = score_parse.normalize_results([_result("A", record_id="rec2")], rows)

    assert dropped == 1 and len(normalized) == 1 and normalized[0]["record_id"] == "rec1"


def test_dropped_not_double_counted() -> None:
    rows = [{"record_id": "rec1", "话题名称": "A"}]
    raw = json.dumps(
        {"scores": [{"话题名称": "A", "gate": "pass", "dimensions": {**_DIMS_4, "可演示性": 9}, "reason": "x"}]},
        ensure_ascii=False,
    )

    items, parsed_dropped, dropped_keys = score_parse.parse_results_full(raw)
    _norm, _dist, norm_dropped = score_parse.normalize_results(items, rows, dropped_keys)

    assert parsed_dropped == 1 and norm_dropped == 0 and parsed_dropped + norm_dropped == 1


def test_reason_with_flags_stays_within_100() -> None:
    items, _ = score_parse.normalize(
        [_result("A", reason="字" * 120, risk_flag=True, source_flag=True)],
        [{"record_id": "rA", "话题名称": "A"}],
    )

    assert len(items[0]["reason"]) == 100 and items[0]["reason"].endswith("…")
