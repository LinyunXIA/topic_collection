from __future__ import annotations

import json

import pytest

from feedkicker import topic as topic_mod
from feedkicker.topic import FILTER_JSON, fetch_selected_topics, fetch_topic_fields


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_import_and_filter_json():
    assert hasattr(topic_mod, "fetch_selected_topics")
    assert hasattr(topic_mod, "fetch_topic_fields")
    assert hasattr(topic_mod, "FILTER_JSON")
    parsed = json.loads(FILTER_JSON)
    assert parsed["logic"] == "and"
    assert any(c[0] == "讨论状态" and "已选题" in str(c) for c in parsed["conditions"])
    assert "intersects" in FILTER_JSON


def test_fetch_selected_server_side_filter(monkeypatch):
    captured: list[list[str]] = []

    def fake_run(args, stdin_text=None, timeout=120):
        captured.append(list(args))
        assert "--filter-json" in args
        idx = args.index("--filter-json")
        payload = json.loads(args[idx + 1])
        assert payload["logic"] == "and"
        assert payload["conditions"][0][0] == "讨论状态"
        assert "已选题" in payload["conditions"][0][2]
        data = {"records": [{"record_id": "recGWg8Kb9kUDI", "fields": {"讨论状态": ["已选题"], "话题名称": "T1"}}]}
        return FakeProc(0, stdout=json.dumps({"data": data}, ensure_ascii=False))

    monkeypatch.setattr(topic_mod.bitable, "_run", fake_run)
    records = fetch_selected_topics("TikpbwV0oaFAnYsoMCxchMRyncr", "tblNPcbupKIBzLAx", limit=200)
    assert len(records) == 1
    assert records[0]["record_id"] == "recGWg8Kb9kUDI"
    assert captured[0][captured[0].index("--base-token") + 1] == "TikpbwV0oaFAnYsoMCxchMRyncr"
    assert captured[0][captured[0].index("--table-id") + 1] == "tblNPcbupKIBzLAx"
    assert "--limit" in captured[0] and "--offset" in captured[0]
    # ensure limit/offset exactly as spec
    assert captured[0][captured[0].index("--limit") + 1] == "200"
    assert captured[0][captured[0].index("--offset") + 1] == "0"


def test_fetch_selected_pagination_merge(monkeypatch):
    calls: list[str] = []

    def fake_run(args, stdin_text=None, timeout=120):
        off = args[args.index("--offset") + 1]
        calls.append(off)
        if off == "0":
            payload = {"records": [
                {"record_id": "rec1", "fields": {"讨论状态": ["已选题"]}},
                {"record_id": "rec2", "fields": {"讨论状态": ["已选题"]}},
            ], "has_more": True}
            return FakeProc(0, stdout=json.dumps({"data": payload}, ensure_ascii=False))
        else:
            payload = {"records": [
                {"record_id": "recGWg8Kb9kUDI", "fields": {"讨论状态": ["已选题"]}},
            ], "has_more": False}
            return FakeProc(0, stdout=json.dumps({"data": payload}, ensure_ascii=False))

    monkeypatch.setattr(topic_mod.bitable, "_run", fake_run)
    records = fetch_selected_topics("app", "tbl", limit=2)
    assert len(records) == 3
    assert calls == ["0", "2"]
    assert any(r["record_id"] == "recGWg8Kb9kUDI" for r in records)


def test_fetch_selected_limit_under_threshold_no_more(monkeypatch):
    def fake_run(args, stdin_text=None, timeout=120):
        payload = {"records": [
            {"record_id": "recA", "fields": {"讨论状态": ["已选题"]}},
        ]}
        return FakeProc(0, stdout=json.dumps({"data": payload}, ensure_ascii=False))

    monkeypatch.setattr(topic_mod.bitable, "_run", fake_run)
    records = fetch_selected_topics("app", "tbl", limit=200)
    assert len(records) == 1


def test_fetch_selected_fields_data_shape(monkeypatch):
    # bitable legacy shape: fields + data (rows as dicts)
    def fake_run(args, stdin_text=None, timeout=120):
        payload = {"fields": ["讨论状态", "话题名称"], "data": [
            {"讨论状态": ["已选题"], "话题名称": "话题A", "record_id": "recX"},
        ]}
        return FakeProc(0, stdout=json.dumps({"data": payload}, ensure_ascii=False))

    monkeypatch.setattr(topic_mod.bitable, "_run", fake_run)
    records = fetch_selected_topics("app", "tbl")
    assert len(records) == 1
    assert records[0]["fields"]["话题名称"] == "话题A"


def test_fetch_selected_failure_127_raises(monkeypatch):
    def fake_run(args, stdin_text=None, timeout=120):
        return FakeProc(127, stdout="", stderr="command not found: lark-cli")

    monkeypatch.setattr(topic_mod.bitable, "_run", fake_run)
    with pytest.raises(RuntimeError, match="127"):
        fetch_selected_topics("app", "tbl")


def test_fetch_selected_not_found_raises(monkeypatch):
    def fake_run(args, stdin_text=None, timeout=120):
        return FakeProc(0, stdout=json.dumps({"ok": False, "error": {"type": "not_found", "message": "table not_found"}}, ensure_ascii=False))

    monkeypatch.setattr(topic_mod.bitable, "_run", fake_run)
    with pytest.raises(RuntimeError):
        fetch_selected_topics("app", "tbl")


def test_fetch_selected_proc_none_raises(monkeypatch):
    monkeypatch.setattr(topic_mod.bitable, "_run", lambda *a, **kw: None)
    with pytest.raises(RuntimeError):
        fetch_selected_topics("app", "tbl")


def test_fetch_selected_empty_token_raises():
    with pytest.raises(ValueError):
        fetch_selected_topics("", "tbl")
    with pytest.raises(ValueError):
        fetch_selected_topics("app", "")


def test_fetch_topic_fields_validates_select(monkeypatch):
    def fake_run(args, stdin_text=None, timeout=60):
        assert "+field-list" in args
        payload = {"fields": [
            {"field_name": "话题名称", "type": "text"},
            {"field_name": "讨论状态", "type": "select"},
        ]}
        return FakeProc(0, stdout=json.dumps({"data": payload}, ensure_ascii=False))

    monkeypatch.setattr(topic_mod.bitable, "_run", fake_run)
    fields = fetch_topic_fields("app", "tbl")
    assert any(f.get("field_name") == "讨论状态" for f in fields)


def test_fetch_topic_fields_failure_raises(monkeypatch):
    monkeypatch.setattr(topic_mod.bitable, "_run", lambda *a, **kw: FakeProc(127, stdout="", stderr="127"))
    with pytest.raises(RuntimeError):
        fetch_topic_fields("app", "tbl")

    monkeypatch.setattr(topic_mod.bitable, "_run", lambda *a, **kw: FakeProc(0, stdout=json.dumps({"ok": False, "error": {"message": "not_found"}})))
    with pytest.raises(RuntimeError):
        fetch_topic_fields("app", "tbl")


def test_fetch_topic_fields_empty_token():
    with pytest.raises(ValueError):
        fetch_topic_fields("", "tbl")


def test_no_full_scan_memory_filter(monkeypatch):
    # ensure no fallback pulls all without filter; if --filter-json missing we would have caught
    def fake_run(args, stdin_text=None, timeout=120):
        if "--filter-json" not in args:
            raise AssertionError("必须服务端 --filter-json 下推，禁止全量拉内存过滤")
        return FakeProc(0, stdout=json.dumps({"data": {"records": []}}, ensure_ascii=False))

    monkeypatch.setattr(topic_mod.bitable, "_run", fake_run)
    records = fetch_selected_topics("app", "tbl")
    assert records == []


def test_cli_dry_run_stub(monkeypatch, capsys):
    import sys
    monkeypatch.setattr(sys, "argv", ["topic", "--env", "test", "--dry-run"])
    def fake_run(args, stdin_text=None, timeout=120):
        payload = {"records": [{"record_id": "recGWg8Kb9kUDI", "fields": {"讨论状态": ["已选题"]}}]}
        return FakeProc(0, stdout=json.dumps({"data": payload}, ensure_ascii=False))

    monkeypatch.setattr(topic_mod.bitable, "_run", fake_run)
    try:
        # run main via exec of module's __main__ logic using runpy
        import runpy
        with pytest.raises(SystemExit) as ei:
            runpy.run_module("feedkicker.topic", run_name="__main__")
        assert ei.value.code == 0
        out = capsys.readouterr().out
        assert "recGWg8Kb9kUDI" in out
    except SystemExit as e:
        if e.code != 0:
            raise
        out = capsys.readouterr().out
        assert "recGWg8Kb9kUDI" in out
