"""wiki_lark.lark_node_list 方法体（#166）：mock bitable._run，断言 node-list argv 与解析分流。"""

from __future__ import annotations

import json

from feedkicker import wiki_lark


class FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_lark_node_list_builds_page_all_argv(monkeypatch) -> None:
    calls: list[tuple[list[str], float]] = []
    proc = FakeProc(0, '{"ok": true, "data": {"nodes": []}}')

    def fake_run(args, stdin_text=None, timeout=120):
        calls.append((list(args), timeout))
        return proc

    monkeypatch.setattr(wiki_lark.bitable, "_run", fake_run)
    got = wiki_lark.lark_node_list("spc1", "parent1")
    assert got is proc
    assert calls == [
        (
            [
                "wiki", "+node-list",
                "--space-id", "spc1",
                "--parent-node-token", "parent1",
                "--page-all",
                "--json",
            ],
            120,
        )
    ]


def test_parse_node_list_extracts_dict_nodes() -> None:
    payload = {"ok": True, "data": {"nodes": [{"node_token": "a"}, "junk", {"node_token": "b"}]}}
    proc = FakeProc(0, json.dumps(payload, ensure_ascii=False))
    assert wiki_lark.parse_node_list(proc) == [{"node_token": "a"}, {"node_token": "b"}]


def test_parse_node_list_failure_shapes_empty() -> None:
    assert wiki_lark.parse_node_list(None) == []
    assert wiki_lark.parse_node_list(FakeProc(1, "{}")) == []
    assert wiki_lark.parse_node_list(FakeProc(0, "")) == []
    assert wiki_lark.parse_node_list(FakeProc(0, "not json")) == []
    assert wiki_lark.parse_node_list(FakeProc(0, '{"data": {"nodes": "oops"}}')) == []
    assert wiki_lark.parse_node_list(FakeProc(0, '{"ok": false, "error": {}}')) == []
