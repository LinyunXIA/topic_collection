"""6 个 __main__ 入口 runpy 冒烟（#166）：确认 guard 真执行、退出码正确；全程离线 mock。

topic 已由 tests/test_topic.py 覆盖，此处覆盖其余 6 个；wiki 的 __main__ 块末尾无 sys.exit。
"""

from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path

import pytest

from feedkicker import bitable_lark, wiki_lark


class FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def run_main(monkeypatch: pytest.MonkeyPatch, module: str, argv: list[str]) -> None:
    monkeypatch.delitem(sys.modules, module, raising=False)
    monkeypatch.setattr(sys, "argv", argv)
    runpy.run_module(module, run_name="__main__")


def test_push_main_smoke_empty_feeds_rc0(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cfg = tmp_path / "empty-feeds.yaml"
    cfg.write_text("feeds: []\n", encoding="utf-8")
    argv = ["tc-push", "--config", str(cfg), "--db", str(tmp_path / "m.sqlite3"), "--env", "test"]
    with pytest.raises(SystemExit) as ei:
        run_main(monkeypatch, "feedkicker.push", argv)
    assert ei.value.code == 0


def test_bitable_main_smoke_dry_run_rc0(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit) as ei:
        run_main(monkeypatch, "feedkicker.bitable", ["tc-bitable", "--dry-run", "--env", "test"])
    assert ei.value.code == 0


def test_purge_main_smoke_dry_run_rc0(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    db = tmp_path / "p.sqlite3"
    with pytest.raises(SystemExit) as ei:
        run_main(monkeypatch, "feedkicker.purge", ["tc-purge", "--env", "test", "--db", str(db)])
    assert ei.value.code == 0
    assert db.exists()


def test_salon_flow_main_smoke_dry_run_rc0(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    argv = ["tc-salon", "--dry-run", "--env", "test", "--db", str(tmp_path / "s.sqlite3")]
    with pytest.raises(SystemExit) as ei:
        run_main(monkeypatch, "feedkicker.salon_flow", argv)
    assert ei.value.code == 0


def test_wiki_main_smoke_dry_run_prints(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    run_main(monkeypatch, "feedkicker.wiki", ["tc-wiki", "--dry-run", "--env", "test", "--title", "冒烟"])
    assert "wiki_url" in capsys.readouterr().out


def test_wiki_main_file_creates_doc_with_file_content(monkeypatch: pytest.MonkeyPatch, tmp_path, capsys) -> None:
    """#199：--file 必须真正建 docx 并打印链接，且内容取自文件。"""
    captured: dict[str, str] = {}

    def fake_run(args, stdin_text=None, timeout=120):
        if list(args[:2]) == ["docs", "+create"]:
            rel = args[args.index("--content") + 1]
            captured["md"] = Path(rel[1:]).read_text(encoding="utf-8")
            return FakeProc(0, stdout=json.dumps({"ok": True, "data": {"document": {"document_id": "docX"}}}))
        if list(args[:2]) == ["wiki", "+node-get"]:
            return FakeProc(0, stdout=json.dumps({"ok": True, "data": {"node": {"node_token": "nodX", "obj_type": "docx"}}}))
        return FakeProc(0, stdout="{}")

    monkeypatch.setattr(bitable_lark, "_run", fake_run)
    md_file = tmp_path / "draft.md"
    md_file.write_text("# 草稿\n- 要点\n", encoding="utf-8")
    run_main(
        monkeypatch,
        "feedkicker.wiki",
        ["tc-wiki", "--file", str(md_file), "--title", "草稿",
         "--app-token", "app", "--space-id", "spc", "--parent-token", "parent"],
    )
    assert captured["md"] == "# 草稿\n- 要点\n"
    assert "wiki/nodX" in capsys.readouterr().out


def test_wiki_home_main_smoke_dry_run_rc0(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
) -> None:
    cfg = tmp_path / "wiki.yaml"
    cfg.write_text("wiki:\n  space_id: spcSmoke\n  parent_token: pnodeSmoke\n", encoding="utf-8")
    monkeypatch.setattr(
        wiki_lark,
        "lark_node_list",
        lambda space_id, parent: FakeProc(0, json.dumps({"ok": True, "data": {"nodes": []}})),
    )
    argv = ["tc-wiki-home", "--dry-run", "--config", str(cfg)]
    with pytest.raises(SystemExit) as ei:
        run_main(monkeypatch, "feedkicker.wiki_home", argv)
    assert ei.value.code == 0
    assert "Wiki 首页预览" in capsys.readouterr().out
