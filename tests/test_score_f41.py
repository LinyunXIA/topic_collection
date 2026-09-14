"""F41：写入列映射 / 只补空与 --force / 分块 / 失败计数 / rc1 / 幂等（全离线，mock lark-cli）。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from feedkicker import bitable_lark, score_flow, score_llm, score_write

_MM_FIELDS = ["话题名称", "可使用工具", "相关AI原理", "资讯链接", "出处来源", "MMax打分", "MMax理由"]

_DIMS = {
    "普适痛点强度": 4.0, "分层承载力": 3.5, "可演示性": 5.0,
    "时效与稀缺": 4.0, "内容复用价值": 3.5, "讲解成本": 2.0,
}


class FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _records(
    n: int, *, start: int = 1, score: str | None = None, reason: str | None = None
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for i in range(n):
        fields: dict[str, object] = {
            "话题名称": f"话题{start + i}", "可使用工具": "工具X", "相关AI原理": "原理Y",
            "资讯链接": "https://a/1", "出处来源": "量子位",
        }
        if score is not None:
            fields["MMax打分"] = score
        if reason is not None:
            fields["MMax理由"] = reason
        rows.append({"record_id": f"rec{start + i}", "fields": fields})
    return rows


def _item(name: str, *, total: object = 4.0, **over: object) -> dict[str, object]:
    item: dict[str, object] = {
        "record_id": f"rec{name}", "话题名称": name, "weighted_total": total, "scores": dict(_DIMS),
        "reason": "依据 可使用工具", "risk_flag": False, "source_flag": False,
    }
    item.update(over)
    return item


def _json_from_args(args: list[str]) -> dict:
    raw = args[args.index("--json") + 1]
    if raw.startswith("@"):
        return json.loads(Path(raw[1:]).read_text(encoding="utf-8"))
    return json.loads(raw)


def _stub_lark(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pages: list[list[dict[str, object]]] | None = None,
    field_names: list[str] | None = None,
    batch_ok: bool = True,
    fail_on: int = 0,
    batch_verb: str | None = "+record-batch-update",
    calls: list[list[str]] | None = None,
    payloads: list[dict] | None = None,
    state: dict | None = None,
) -> None:
    counter = {"batch": 0}

    def fake_run(args, stdin_text=None, timeout=120):
        if calls is not None:
            calls.append(list(args))
        if args[:2] == ["base", "--help"]:
            return FakeProc(0, batch_verb or "")
        if "+field-list" in args:
            payload = {"fields": [{"field_name": n} for n in (field_names or [])]}
            return FakeProc(0, json.dumps({"data": payload}, ensure_ascii=False))
        if "+record-list" in args:
            offset = int(args[args.index("--offset") + 1])
            idx = offset // bitable_lark._CHUNK
            src = state["records"] if state is not None else (pages[idx] if pages and idx < len(pages) else [])
            return FakeProc(0, json.dumps({"data": {"records": src}}, ensure_ascii=False))
        if "+record-batch-update" in args:
            counter["batch"] += 1
            payload = _json_from_args(args)["update_records"]
            if payloads is not None:
                payloads.append(payload)
            if state is not None:
                for rec in state["records"]:
                    if rec["record_id"] in payload:
                        rec["fields"].update(payload[rec["record_id"]])
            ok = batch_ok and counter["batch"] != fail_on
            return FakeProc(0, "{}" if ok else '{"ok": false, "error": {"message": "boom"}}')
        if "+record-update" in args:
            counter["batch"] += 1
            if payloads is not None:
                payloads.append(_json_from_args(args))
            return FakeProc(0, "{}")
        return FakeProc(0, "{}")

    monkeypatch.setattr(bitable_lark, "_run", fake_run)


def _stub_llm_echo(monkeypatch: pytest.MonkeyPatch) -> None:
    import re

    def fake(conf, prompt):
        names = [n.strip() for n in re.findall(r"^\d+\. 话题名称：(.+)$", prompt, re.MULTILINE)]
        results = [
            {
                "话题名称": n, "gate": "pass", "dimensions": dict(_DIMS), "missing": [],
                "weighted_total": 4.0, "risk_flag": False, "source_flag": False, "reason": "依据 可使用工具",
            }
            for n in names
        ]
        return json.dumps({"scores": results}, ensure_ascii=False)

    monkeypatch.setattr(score_llm, "call_llm", fake)


def _write_cfg(tmp_path: Path, *, provider: str = "minimax", batch_size: int = 100) -> Path:
    path = tmp_path / "f41-cfg.yaml"
    path.write_text(
        "salon:\n  app_token: \"appTest\"\n  table_id: \"tblTest\"\n"
        "score:\n  enabled: true\n"
        f"  provider: {provider}\n"
        "  prompt_file: prompts/score.md\n"
        f"  batch_size: {batch_size}\n  providers:\n    {provider}:\n      api_key: \"sk-test\"\n",
        encoding="utf-8",
    )
    return path


def _args(cfg: Path, tmp_path: Path, *extra: str) -> list[str]:
    return [*extra, "--config", str(cfg), "--db", str(tmp_path / "t.sqlite3")]


def _summary(out: str) -> dict:
    return json.loads([ln for ln in out.splitlines() if ln.startswith("{")][-1])


def test_plan_writes_only_fills_empty() -> None:
    scored = [_item("A", 打分="3.5"), _item("B", 打分=None), _item("C")]

    to_write, skipped = score_write.plan_writes(scored, force=False)

    assert [i["话题名称"] for i in to_write] == ["B", "C"]
    assert [i["话题名称"] for i in skipped] == ["A"]


def test_plan_writes_force_all() -> None:
    scored = [_item("A", 打分="3.5"), _item("B")]

    to_write, skipped = score_write.plan_writes(scored, force=True)

    assert len(to_write) == 2 and skipped == []


def test_plan_writes_reason_only_still_writes() -> None:
    scored = [_item("A", 打分="", 理由="旧理由")]

    to_write, skipped = score_write.plan_writes(scored, force=False)

    assert len(to_write) == 1 and skipped == []
    cell = score_write._cell(to_write[0], "MMax")
    assert set(cell) == {"MMax打分", "MMax理由"}
    assert cell["MMax打分"] == "4.0" and cell["MMax理由"].startswith("依据 可使用工具")


def test_write_cell_maps_only_two_provider_columns() -> None:
    cell = score_write._cell(_item("A"), "MMax")

    assert set(cell) == {"MMax打分", "MMax理由"}
    assert cell["MMax打分"] == "4.0"


def test_write_cell_deepseek_label() -> None:
    cell = score_write._cell(_item("A"), "DS")

    assert set(cell) == {"DS打分", "DS理由"}


def test_score_cell_one_decimal_and_missing() -> None:
    assert score_write._cell(_item("A", total=3.5), "MMax")["MMax打分"] == "3.5"
    assert score_write._cell(_item("A", total=4), "MMax")["MMax打分"] == "4.0"
    assert score_write._cell(_item("A", total=0.0), "MMax")["MMax打分"] == "0.0"
    assert score_write._cell(_item("A", total=None), "MMax")["MMax打分"] == "缺失"


def test_reason_cell_single_line_flags_and_dims() -> None:
    cell = score_write._cell(_item("A", reason="引用 可使用工具", risk_flag=True), "MMax")
    detail = cell["MMax理由"]

    assert "\n" not in detail
    assert "引用 可使用工具 ｜ risk=true ｜ source=false ｜ 六维：" in detail
    assert detail.endswith("普适痛点4.0/分层承载3.5/可演示5.0/时效稀缺4.0/复用价值3.5/讲解成本2.0")


def test_reason_cell_missing_dim_marked() -> None:
    cell = score_write._cell(_item("A", scores={"普适痛点强度": 4.0, "分层承载力": "缺失"}), "MMax")

    assert "普适痛点4.0/分层承载缺失/" in cell["MMax理由"]


def test_write_scores_chunks_at_most_100(monkeypatch) -> None:
    calls: list[list[str]] = []
    payloads: list[dict] = []
    _stub_lark(monkeypatch, field_names=_MM_FIELDS, calls=calls, payloads=payloads)
    rows = [_item(f"T{i}") for i in range(250)]

    stats = score_write.write_scores(score_write.WriteConf("appTest", "tblTest", "minimax"), rows, dry_run=False)

    batches = [c for c in calls if "+record-batch-update" in c]
    assert len(batches) == 3 and stats.written == 250 and stats.failed_writes == 0
    assert [len(p) for p in payloads] == [100, 100, 50]


def test_write_scores_payload_never_touches_other_columns(monkeypatch) -> None:
    payloads: list[dict] = []
    _stub_lark(monkeypatch, field_names=_MM_FIELDS, payloads=payloads)

    score_write.write_scores(score_write.WriteConf("appTest", "tblTest", "minimax"), [_item("A")], dry_run=False)

    assert set(payloads[0]["recA"]) == {"MMax打分", "MMax理由"}


def test_write_scores_dry_run_zero_calls(monkeypatch) -> None:
    calls: list[list[str]] = []
    _stub_lark(monkeypatch, field_names=_MM_FIELDS, calls=calls)

    stats = score_write.write_scores(score_write.WriteConf("appTest", "tblTest", "minimax"), [_item("A")], dry_run=True)

    assert calls == [] and stats.written == 0 and stats.scored == 1


def test_write_scores_failure_increments_failed_writes(monkeypatch) -> None:
    _stub_lark(monkeypatch, field_names=_MM_FIELDS, batch_ok=False)

    stats = score_write.write_scores(
        score_write.WriteConf("appTest", "tblTest", "minimax"), [_item("A"), _item("B")], dry_run=False
    )

    assert stats.written == 0 and stats.failed_writes == 2


def test_write_scores_single_fallback_without_batch_verb(monkeypatch) -> None:
    calls: list[list[str]] = []
    _stub_lark(monkeypatch, field_names=_MM_FIELDS, batch_verb="+record-update", calls=calls)

    stats = score_write.write_scores(
        score_write.WriteConf("appTest", "tblTest", "minimax"), [_item("A"), _item("B")], dry_run=False
    )

    assert stats.written == 2
    assert len([c for c in calls if "+record-update" in c]) == 2


def test_write_scores_no_verb_raises(monkeypatch) -> None:
    _stub_lark(monkeypatch, field_names=_MM_FIELDS, batch_verb=None)

    with pytest.raises(RuntimeError, match="无法写表"):
        score_write.write_scores(score_write.WriteConf("appTest", "tblTest", "minimax"), [_item("A")], dry_run=False)


def test_run_dry_run_zero_write_calls(tmp_path, monkeypatch, capsys) -> None:
    cfg = _write_cfg(tmp_path)
    calls: list[list[str]] = []
    _stub_lark(monkeypatch, pages=[_records(2)], field_names=_MM_FIELDS, calls=calls)
    _stub_llm_echo(monkeypatch)

    rc = score_flow.main(_args(cfg, tmp_path))

    summary = _summary(capsys.readouterr().out)
    assert rc == 0 and summary["written"] == 0
    assert not any("+record-batch-update" in c or "+record-update" in c for c in calls)


def test_run_apply_writes_and_summary(tmp_path, monkeypatch, capsys) -> None:
    cfg = _write_cfg(tmp_path)
    calls: list[list[str]] = []
    _stub_lark(monkeypatch, pages=[_records(2)], field_names=_MM_FIELDS, calls=calls)
    _stub_llm_echo(monkeypatch)

    rc = score_flow.main(_args(cfg, tmp_path, "--apply"))

    summary = _summary(capsys.readouterr().out)
    assert rc == 0 and summary["mode"] == "apply" and summary["written"] == 2
    assert summary["failed_writes"] == 0 and summary["skipped"] == 0


def test_run_apply_all_failure_rc1(tmp_path, monkeypatch, caplog, capsys) -> None:
    cfg = _write_cfg(tmp_path)
    _stub_lark(monkeypatch, pages=[_records(2)], field_names=_MM_FIELDS, batch_ok=False)
    _stub_llm_echo(monkeypatch)

    with caplog.at_level(logging.ERROR):
        rc = score_flow.main(_args(cfg, tmp_path, "--apply"))

    summary = _summary(capsys.readouterr().out)
    assert rc == 1 and summary["written"] == 0 and summary["failed_writes"] == 2
    assert "写入全部失败" in caplog.text


def test_run_apply_partial_failure_rc0(tmp_path, monkeypatch) -> None:
    cfg = _write_cfg(tmp_path)
    monkeypatch.setattr(score_write, "WRITE_CHUNK", 1)
    _stub_lark(monkeypatch, pages=[_records(2)], field_names=_MM_FIELDS, fail_on=2)
    _stub_llm_echo(monkeypatch)

    rc = score_flow.main(_args(cfg, tmp_path, "--apply"))

    assert rc == 0


def test_run_apply_idempotent_second_run(tmp_path, monkeypatch, capsys) -> None:
    cfg = _write_cfg(tmp_path)
    state = {"records": _records(2)}
    _stub_lark(monkeypatch, field_names=_MM_FIELDS, state=state)
    _stub_llm_echo(monkeypatch)

    first = score_flow.main(_args(cfg, tmp_path, "--apply"))
    first_summary = _summary(capsys.readouterr().out)
    second = score_flow.main(_args(cfg, tmp_path, "--apply"))
    second_summary = _summary(capsys.readouterr().out)

    assert first == 0 and first_summary["written"] == 2
    assert second == 0 and second_summary["written"] == 0 and second_summary["skipped"] == 2


def test_run_apply_force_rewrites_scored_rows(tmp_path, monkeypatch, capsys) -> None:
    cfg = _write_cfg(tmp_path)
    state = {"records": _records(2, score="1.0")}
    _stub_lark(monkeypatch, field_names=_MM_FIELDS, state=state)
    _stub_llm_echo(monkeypatch)

    rc = score_flow.main(_args(cfg, tmp_path, "--apply", "--force"))

    summary = _summary(capsys.readouterr().out)
    assert rc == 0 and summary["written"] == 2 and summary["skipped"] == 0


def test_run_existing_scores_skipped_without_llm(tmp_path, monkeypatch, capsys) -> None:
    cfg = _write_cfg(tmp_path)
    _stub_lark(monkeypatch, pages=[_records(2, score="3.5")], field_names=_MM_FIELDS)
    prompts: list[str] = []

    def fake(conf, prompt):
        prompts.append(prompt)
        return '{"scores": []}'

    monkeypatch.setattr(score_llm, "call_llm", fake)

    rc = score_flow.main(_args(cfg, tmp_path, "--apply"))

    summary = _summary(capsys.readouterr().out)
    assert rc == 0 and prompts == [] and summary["skipped"] == 2 and summary["written"] == 0


def test_run_apply_rescores_reason_only_row(tmp_path, monkeypatch, capsys) -> None:
    cfg = _write_cfg(tmp_path)
    state = {"records": _records(1, reason="旧理由")}
    payloads: list[dict] = []
    _stub_lark(monkeypatch, field_names=_MM_FIELDS, state=state, payloads=payloads)
    _stub_llm_echo(monkeypatch)

    rc = score_flow.main(_args(cfg, tmp_path, "--apply"))

    summary = _summary(capsys.readouterr().out)
    assert rc == 0 and summary["written"] == 1 and summary["skipped"] == 0
    assert set(payloads[0]["rec1"]) == {"MMax打分", "MMax理由"}
    assert state["records"][0]["fields"]["MMax打分"] == "3.9"
    assert state["records"][0]["fields"]["MMax理由"] != "旧理由"


def test_run_missing_columns_rc2_before_write(tmp_path, monkeypatch, caplog) -> None:
    cfg = _write_cfg(tmp_path)
    calls: list[list[str]] = []
    _stub_lark(monkeypatch, pages=[_records(1)], field_names=["话题名称", "MMax理由"], calls=calls)
    _stub_llm_echo(monkeypatch)

    with caplog.at_level(logging.ERROR):
        rc = score_flow.main(_args(cfg, tmp_path, "--apply"))

    assert rc == 2 and "目标列缺失" in caplog.text
    assert not any("+record-batch-update" in c for c in calls)


def test_run_apply_deepseek_labels(tmp_path, monkeypatch, capsys) -> None:
    cfg = _write_cfg(tmp_path, provider="deepseek")
    ds_fields = ["话题名称", "可使用工具", "相关AI原理", "资讯链接", "出处来源", "DS打分", "DS理由"]
    payloads: list[dict] = []
    _stub_lark(monkeypatch, pages=[_records(1)], field_names=ds_fields, payloads=payloads)
    _stub_llm_echo(monkeypatch)

    rc = score_flow.main(_args(cfg, tmp_path, "--apply", "--provider", "deepseek"))

    assert rc == 0 and set(payloads[0]["rec1"]) == {"DS打分", "DS理由"}


def test_run_summary_keys(tmp_path, monkeypatch, capsys) -> None:
    cfg = _write_cfg(tmp_path)
    _stub_lark(monkeypatch, pages=[_records(1)], field_names=_MM_FIELDS)
    _stub_llm_echo(monkeypatch)

    rc = score_flow.main(_args(cfg, tmp_path, "--apply"))

    summary = _summary(capsys.readouterr().out)
    assert rc == 0
    assert set(summary) == {
        "mode", "batches", "llm_calls", "rows", "scored", "skipped", "written",
        "failed_writes", "failed_batches", "dropped", "empty_batches",
    }


def test_run_empty_table_rc0_zero_stats(tmp_path, monkeypatch, capsys) -> None:
    cfg = _write_cfg(tmp_path)
    calls: list[list[str]] = []
    _stub_lark(monkeypatch, pages=[[]], field_names=_MM_FIELDS, calls=calls)
    _stub_llm_echo(monkeypatch)

    rc = score_flow.main(_args(cfg, tmp_path, "--apply"))

    summary = _summary(capsys.readouterr().out)
    assert rc == 0 and summary["rows"] == 0 and summary["written"] == 0
    assert not any("+record-batch-update" in c for c in calls)


def test_stats_dataclass_fields() -> None:
    stats = score_write.ScoreStats()

    assert stats.rows == 0 and stats.scored == 0 and stats.skipped == 0
    assert stats.written == 0 and stats.failed_writes == 0
    assert stats.failed_batches == 0 and stats.dropped == 0 and stats.empty_batches == 0
