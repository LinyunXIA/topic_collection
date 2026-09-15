"""第四轮审计 P3（#227/#228/#229）。

#227：minimax 响应形态归一、topic has_more 字符串、check-fields stub 提示、wiki --file 报错；
#229：互斥 flag、dry-run 措辞、死代码、salon 全失败告警。全部离线 mock。
"""

from __future__ import annotations

import json
import logging
import runpy
import sys

import pytest

import feedkicker.minimax as mm
import feedkicker.topic as topic_mod
from feedkicker import bitable, bitable_lark, salon_flow, salon_notify, store, wiki
from feedkicker.config import PROJECT_ROOT, Config, load_config
from feedkicker.topic import fetch_selected_topics


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeResp:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = json.dumps(self._json, ensure_ascii=False)

    def json(self):
        return self._json


# ── #227(a) minimax 响应类型归一 ──


@pytest.mark.parametrize(
    "payload",
    [
        {"choices": "raw-string"},
        {"choices": {"a": 1}},
        {"choices": 3},
        {"choices": None},
        {"choices": [None]},
        {"choices": [{"message": "not-a-dict"}]},
        {"choices": [{"message": {"tool_calls": "x"}}]},
        {"choices": [{"message": {"tool_calls": [None]}}]},
        {"choices": [{"message": {"tool_calls": [{"function": "x"}]}}]},
    ],
)
def test_parse_outline_type_shapes_raise_runtime(payload):
    import json as _json

    with pytest.raises(RuntimeError, match="MiniMax|choices|tool_calls|content|响应"):
        mm._parse_outline_from_response(_json.loads(_json.dumps(payload)))


def test_call_minimax_chat_unhashable_base_resp_status_code(monkeypatch):
    monkeypatch.setattr(mm.httpx, "post", lambda *a, **k: FakeResp(200, {"base_resp": {"status_code": []}}))
    with pytest.raises(RuntimeError, match="status_code|类型"):
        mm.call_minimax_chat([{"role": "user", "content": "hi"}], api_key="sk")


def test_call_minimax_chat_unhashable_code_no_typeerror(monkeypatch):
    monkeypatch.setattr(mm.httpx, "post", lambda *a, **k: FakeResp(200, {"code": []}))
    with pytest.raises(RuntimeError):
        mm.gen_outline("topic", kind="tool", api_key="sk")


def test_extract_code_normalizes_unhashable():
    assert mm._extract_code({"code": []}) is None
    assert mm._extract_code({"base_resp": {"status_code": {}}}) is None


# ── #227(b) topic has_more 归一 ──


def test_has_more_string_false_stops_pagination(monkeypatch):
    calls: list[str] = []

    def fake_run(args, stdin_text=None, timeout=120):
        calls.append(args[args.index("--offset") + 1])
        records = [{"record_id": f"r{i}", "fields": {}} for i in range(200)]
        return FakeProc(0, json.dumps({"data": {"records": records, "has_more": "false"}}))

    monkeypatch.setattr(topic_mod.bitable_lark, "_run", fake_run)
    out = fetch_selected_topics("app", "tbl")
    assert len(out) == 200
    assert calls == ["0"], 'has_more "false" 必须归一为 False 而非真值'


def test_has_more_string_true_continues(monkeypatch):
    state = {"n": 0}

    def fake_run(args, stdin_text=None, timeout=120):
        state["n"] += 1
        more = "true" if state["n"] == 1 else "false"
        return FakeProc(0, json.dumps({"data": {"records": [{"record_id": f"r{state['n']}", "fields": {}}], "has_more": more}}))

    monkeypatch.setattr(topic_mod.bitable_lark, "_run", fake_run)
    out = fetch_selected_topics("app", "tbl", limit=1)
    assert [r["record_id"] for r in out] == ["r1", "r2"]


# ── #227(c) topic --dry-run --check-fields 且 token 缺失 ──


def test_topic_cli_check_fields_stub_warns(monkeypatch, capsys, caplog):
    monkeypatch.delitem(sys.modules, "feedkicker.topic", raising=False)
    monkeypatch.setattr(sys, "argv", ["topic", "--env", "test", "--dry-run", "--check-fields"])
    with caplog.at_level(logging.WARNING):
        with pytest.raises(SystemExit) as ei:
            runpy.run_module("feedkicker.topic", run_name="__main__")
    assert ei.value.code == 0
    assert "recStub000" in capsys.readouterr().out
    assert any("check-fields" in r.getMessage() for r in caplog.records), "stub 分支必须提示 check-fields 被跳过"


# ── #227(d) wiki --file 读取失败 ──


def test_wiki_cli_file_missing_returns_2(monkeypatch, caplog):
    cfg = Config(app_env="test")
    monkeypatch.setattr("feedkicker.config.load_config", lambda *a, **kw: cfg)
    with caplog.at_level(logging.ERROR):
        rc = wiki.main(
            ["--file", "/nonexistent/audit4-nope.md", "--env", "test",
             "--app-token", "a", "--space-id", "s", "--parent-token", "p"]
        )
    assert rc == 2
    assert any("--file" in r.getMessage() for r in caplog.records)


# ── #229 互斥 flag 与 dry-run 措辞 ──


@pytest.mark.parametrize(
    "argv",
    [
        ["--reseed", "--backfill"],
        ["--reseed", "--fix-archive-date"],
        ["--backfill", "--fix-archive-date"],
    ],
)
def test_bitable_reseed_backfill_mutually_exclusive(argv):
    with pytest.raises(SystemExit) as ei:
        bitable.main([*argv, "--env", "test"])
    assert ei.value.code == 2


def test_reseed_dry_run_log_mentions_first_screen(monkeypatch, tmp_path, caplog):
    page = "| _record_id | 标题 |\n| --- | --- |\n| recAAA | a |"
    cfg = Config(app_env="prod")
    cfg.bitable.enabled = True
    cfg.bitable.app_token = "appReal"
    cfg.bitable.table_id = "tblReal"
    cfg.db_path = str(tmp_path / "p.sqlite3")
    monkeypatch.setattr("feedkicker.config.load_config", lambda *a, **kw: cfg)
    monkeypatch.setattr(bitable_lark, "_run", lambda *a, **k: FakeProc(0, stdout=page))
    with caplog.at_level(logging.INFO):
        assert bitable.main(["--reseed", "--dry-run", "--env", "prod"]) == 0
    assert any("首屏" in r.getMessage() for r in caplog.records), "dry-run 措辞需注明仅数首屏"


# ── #229 死代码与全失败告警 ──


def test_salon_flow_stub_dead_assignments_removed():
    src = (PROJECT_ROOT / "feedkicker" / "salon_flow.py").read_text(encoding="utf-8")
    assert "stub_app" not in src and "stub_tbl" not in src


def test_salon_notify_empty_urls_warns(caplog):
    cfg = load_config(app_env="test")
    conn = store.connect(":memory:")
    with caplog.at_level(logging.WARNING):
        assert salon_notify.send_wiki_card(cfg, conn, []) is True
    assert any("0 条成功" in r.getMessage() or "无 Wiki" in r.getMessage() for r in caplog.records)
    conn.close()


def test_salon_flow_all_topics_fail_warns_rc1(monkeypatch, caplog):
    cfg = load_config(app_env="test")
    cfg.salon.enabled = True
    cfg.salon.app_token = "appTokenTest"
    cfg.salon.table_id = "tblTest"
    cfg.wiki.space_id = "spc_test"
    cfg.wiki.parent_token = "parent_test"
    cfg.wiki.app_token = "app_wiki"
    cfg.minimax.api_key = "sk-test"
    cfg.feishu_webhook = "https://hook.test"
    conn = store.connect(":memory:")
    monkeypatch.setattr(salon_flow, "fetch_selected_topics", lambda *a, **k: [
        {"record_id": "rec1", "fields": {"讨论状态": ["已选题"], "话题名称": "T1"}},
    ])
    monkeypatch.setattr(
        salon_flow.minimax,
        "gen_outline",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("MiniMax 429")),
    )
    with caplog.at_level(logging.WARNING):
        assert salon_flow.run(cfg, conn, dry_run=False) == 1
    assert any("0 条成功" in r.getMessage() for r in caplog.records), "全失败需告警且 rc1（#R9-27）"
    conn.close()
