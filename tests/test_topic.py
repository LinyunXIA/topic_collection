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
        data = {"records": [{"record_id": "recStub000", "fields": {"讨论状态": ["已选题"], "话题名称": "T1"}}]}
        return FakeProc(0, stdout=json.dumps({"data": data}, ensure_ascii=False))

    monkeypatch.setattr(topic_mod.bitable_lark, "_run", fake_run)
    records = fetch_selected_topics("appTokenTest", "tblTest", limit=200)
    assert len(records) == 1
    assert records[0]["record_id"] == "recStub000"
    assert captured[0][captured[0].index("--base-token") + 1] == "appTokenTest"
    assert captured[0][captured[0].index("--table-id") + 1] == "tblTest"
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
                {"record_id": "recStub000", "fields": {"讨论状态": ["已选题"]}},
            ], "has_more": False}
            return FakeProc(0, stdout=json.dumps({"data": payload}, ensure_ascii=False))

    monkeypatch.setattr(topic_mod.bitable_lark, "_run", fake_run)
    records = fetch_selected_topics("app", "tbl", limit=2)
    assert len(records) == 3
    assert calls == ["0", "2"]
    assert any(r["record_id"] == "recStub000" for r in records)


def test_fetch_selected_limit_under_threshold_no_more(monkeypatch):
    def fake_run(args, stdin_text=None, timeout=120):
        payload = {"records": [
            {"record_id": "recA", "fields": {"讨论状态": ["已选题"]}},
        ]}
        return FakeProc(0, stdout=json.dumps({"data": payload}, ensure_ascii=False))

    monkeypatch.setattr(topic_mod.bitable_lark, "_run", fake_run)
    records = fetch_selected_topics("app", "tbl", limit=200)
    assert len(records) == 1


def test_fetch_selected_fields_data_shape(monkeypatch):
    # bitable legacy shape: fields + data (rows as dicts)
    def fake_run(args, stdin_text=None, timeout=120):
        payload = {"fields": ["讨论状态", "话题名称"], "data": [
            {"讨论状态": ["已选题"], "话题名称": "话题A", "record_id": "recX"},
        ]}
        return FakeProc(0, stdout=json.dumps({"data": payload}, ensure_ascii=False))

    monkeypatch.setattr(topic_mod.bitable_lark, "_run", fake_run)
    records = fetch_selected_topics("app", "tbl")
    assert len(records) == 1
    assert records[0]["fields"]["话题名称"] == "话题A"


def test_fetch_selected_failure_127_raises(monkeypatch):
    def fake_run(args, stdin_text=None, timeout=120):
        return FakeProc(127, stdout="", stderr="command not found: lark-cli")

    monkeypatch.setattr(topic_mod.bitable_lark, "_run", fake_run)
    with pytest.raises(RuntimeError, match="127"):
        fetch_selected_topics("app", "tbl")


def test_fetch_selected_not_found_raises(monkeypatch):
    def fake_run(args, stdin_text=None, timeout=120):
        return FakeProc(0, stdout=json.dumps({"ok": False, "error": {"type": "not_found", "message": "table not_found"}}, ensure_ascii=False))

    monkeypatch.setattr(topic_mod.bitable_lark, "_run", fake_run)
    with pytest.raises(RuntimeError):
        fetch_selected_topics("app", "tbl")


def test_fetch_selected_proc_none_raises(monkeypatch):
    monkeypatch.setattr(topic_mod.bitable_lark, "_run", lambda *a, **kw: None)
    with pytest.raises(RuntimeError):
        fetch_selected_topics("app", "tbl")


def test_fetch_selected_empty_token_raises():
    with pytest.raises(ValueError):
        fetch_selected_topics("", "tbl")
    with pytest.raises(ValueError):
        fetch_selected_topics("app", "")


def test_fetch_selected_empty_pages_with_has_more_terminates(monkeypatch):
    # #290：has_more 恒真 + 连续空页无页指纹可判，由空页熔断（第 2 页）中止，不再靠 20 万 offset 兜底
    calls = {"n": 0}

    def fake_run(args, stdin_text=None, timeout=120):
        calls["n"] += 1
        assert calls["n"] <= 3, "分页未在空页熔断内终止（死循环）"
        return FakeProc(0, stdout=json.dumps({"data": {"records": [], "has_more": True}}))

    monkeypatch.setattr(topic_mod.bitable_lark, "_run", fake_run)
    with pytest.raises(RuntimeError, match="分页|空页"):
        fetch_selected_topics("app", "tbl")
    assert calls["n"] == 2


def test_fetch_selected_single_empty_page_then_recovers(monkeypatch):
    """#290 对照：单空页容忍，下一轮恢复出数据须正常返回记录，不得过度熔断。"""
    offsets: list[str] = []

    def fake_run(args, stdin_text=None, timeout=120):
        off = args[args.index("--offset") + 1]
        offsets.append(off)
        if off == "0":
            payload = {"records": [], "has_more": True}
        else:
            payload = {
                "records": [{"record_id": "recR", "fields": {"讨论状态": ["已选题"]}}],
                "has_more": False,
            }
        return FakeProc(0, stdout=json.dumps({"data": payload}, ensure_ascii=False))

    monkeypatch.setattr(topic_mod.bitable_lark, "_run", fake_run)
    records = fetch_selected_topics("app", "tbl", limit=200)
    assert [r["record_id"] for r in records] == ["recR"]
    assert offsets == ["0", "200"]


def test_fetch_selected_limit_zero_clamped(monkeypatch):
    """#210：limit<=0 会令 offset 守卫失效，必须钳到 1。"""
    captured: list[list[str]] = []

    def fake_run(args, stdin_text=None, timeout=120):
        captured.append(list(args))
        return FakeProc(0, stdout=json.dumps({"data": {"records": [], "has_more": False}}, ensure_ascii=False))

    monkeypatch.setattr(topic_mod.bitable_lark, "_run", fake_run)
    assert fetch_selected_topics("app", "tbl", limit=0) == []
    assert captured[0][captured[0].index("--limit") + 1] == "1"


def test_topic_cli_limit_zero_rejected(monkeypatch):
    import runpy
    import sys

    monkeypatch.delitem(sys.modules, "feedkicker.topic", raising=False)
    monkeypatch.setattr(sys, "argv", ["topic", "--env", "test", "--limit", "0"])
    with pytest.raises(SystemExit) as ei:
        runpy.run_module("feedkicker.topic", run_name="__main__")
    assert ei.value.code == 2


def test_fetch_topic_fields_type_four_multi_select_ok(monkeypatch, caplog):
    """#210：多选数字码 "4" 属合法类型，不得告警。"""
    payload = {"data": {"fields": [{"field_name": "讨论状态", "type": "4"}]}}

    def fake_run(args, stdin_text=None, timeout=60):
        assert "+field-list" in args
        return FakeProc(0, stdout=json.dumps(payload, ensure_ascii=False))

    monkeypatch.setattr(topic_mod.bitable_lark, "_run", fake_run)
    with caplog.at_level("WARNING"):
        fields = fetch_topic_fields("app", "tbl")
    assert fields
    assert not [r for r in caplog.records if "字段类型异常" in r.getMessage()]


def test_fetch_topic_fields_validates_select(monkeypatch):
    def fake_run(args, stdin_text=None, timeout=60):
        assert "+field-list" in args
        payload = {"fields": [
            {"field_name": "话题名称", "type": "text"},
            {"field_name": "讨论状态", "type": "select"},
        ]}
        return FakeProc(0, stdout=json.dumps({"data": payload}, ensure_ascii=False))

    monkeypatch.setattr(topic_mod.bitable_lark, "_run", fake_run)
    fields = fetch_topic_fields("app", "tbl")
    assert any(f.get("field_name") == "讨论状态" for f in fields)


def test_fetch_topic_fields_failure_raises(monkeypatch):
    monkeypatch.setattr(topic_mod.bitable_lark, "_run", lambda *a, **kw: FakeProc(127, stdout="", stderr="127"))
    with pytest.raises(RuntimeError):
        fetch_topic_fields("app", "tbl")

    monkeypatch.setattr(topic_mod.bitable_lark, "_run", lambda *a, **kw: FakeProc(0, stdout=json.dumps({"ok": False, "error": {"message": "not_found"}})))
    with pytest.raises(RuntimeError):
        fetch_topic_fields("app", "tbl")


def test_fetch_topic_fields_empty_token():
    with pytest.raises(ValueError):
        fetch_topic_fields("", "tbl")


@pytest.mark.parametrize("bad_fields", ["oops", [1, 2], {"a": 1}])
def test_fetch_topic_fields_non_list_dict_raises(monkeypatch, bad_fields):
    """#276：fields 为 str / list[str] / dict 时统一 RuntimeError，不得裸 AttributeError。"""

    def fake_run(args, stdin_text=None, timeout=60):
        return FakeProc(0, stdout=json.dumps({"data": {"fields": bad_fields}}, ensure_ascii=False))

    monkeypatch.setattr(topic_mod.bitable_lark, "_run", fake_run)
    with pytest.raises(RuntimeError, match="fields"):
        fetch_topic_fields("app", "tbl")


def test_has_more_accepts_string_one():
    """#277：has_more 字符串 "1" 视为真。"""
    assert topic_mod._has_more_of({"has_more": "1"}) is True
    assert topic_mod._has_more_of({"has_more": "true"}) is True
    assert topic_mod._has_more_of({"has_more": "false"}) is False


def test_has_more_null_falls_back_to_camel():
    """#277：has_more 为 null 时回退看 hasMore。"""
    assert topic_mod._has_more_of({"has_more": None, "hasMore": True}) is True
    assert topic_mod._has_more_of({"has_more": None, "hasMore": False}) is False
    assert topic_mod._has_more_of({}) is None


def test_fetch_selected_short_page_advances_by_len(monkeypatch):
    """#277：has_more=true 且本页 < limit 时须按 len(chunk) 前进，不得按 limit 跳记录。"""
    offsets: list[str] = []

    def fake_run(args, stdin_text=None, timeout=120):
        off = args[args.index("--offset") + 1]
        offsets.append(off)
        if off == "0":
            payload = {
                "records": [
                    {"record_id": "recA", "fields": {"讨论状态": ["已选题"]}},
                    {"record_id": "recB", "fields": {"讨论状态": ["已选题"]}},
                    {"record_id": "recC", "fields": {"讨论状态": ["已选题"]}},
                ],
                "has_more": True,
            }
        else:
            payload = {
                "records": [
                    {"record_id": "recD", "fields": {"讨论状态": ["已选题"]}},
                    {"record_id": "recE", "fields": {"讨论状态": ["已选题"]}},
                ],
                "has_more": False,
            }
        return FakeProc(0, stdout=json.dumps({"data": payload}, ensure_ascii=False))

    monkeypatch.setattr(topic_mod.bitable_lark, "_run", fake_run)
    records = fetch_selected_topics("app", "tbl", limit=10)

    assert offsets == ["0", "3"]
    assert [r["record_id"] for r in records] == ["recA", "recB", "recC", "recD", "recE"]


def test_no_full_scan_memory_filter(monkeypatch):
    # ensure no fallback pulls all without filter; if --filter-json missing we would have caught
    def fake_run(args, stdin_text=None, timeout=120):
        if "--filter-json" not in args:
            raise AssertionError("必须服务端 --filter-json 下推，禁止全量拉内存过滤")
        return FakeProc(0, stdout=json.dumps({"data": {"records": []}}, ensure_ascii=False))

    monkeypatch.setattr(topic_mod.bitable_lark, "_run", fake_run)
    records = fetch_selected_topics("app", "tbl")
    assert records == []


def test_cli_dry_run_stub(monkeypatch, capsys):
    import sys
    monkeypatch.delitem(sys.modules, "feedkicker.topic", raising=False)
    monkeypatch.setattr(sys, "argv", ["topic", "--env", "test", "--dry-run"])
    def fake_run(args, stdin_text=None, timeout=120):
        payload = {"records": [{"record_id": "recStub000", "fields": {"讨论状态": ["已选题"]}}]}
        return FakeProc(0, stdout=json.dumps({"data": payload}, ensure_ascii=False))

    monkeypatch.setattr(topic_mod.bitable_lark, "_run", fake_run)
    try:
        # run main via exec of module's __main__ logic using runpy
        import runpy
        with pytest.raises(SystemExit) as ei:
            runpy.run_module("feedkicker.topic", run_name="__main__")
        assert ei.value.code == 0
        out = capsys.readouterr().out
        assert "recStub000" in out
    except SystemExit as e:
        if e.code != 0:
            raise
        out = capsys.readouterr().out
        assert "recStub000" in out
