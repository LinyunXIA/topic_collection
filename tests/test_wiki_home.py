"""Wiki 首页自动索引测试（#137 / F23）。

全部 lark-cli 子进程经 monkeypatch 拦截（bitable._run 或 wiki_lark 薄封装），
无真实网络/子进程、无真实 Wiki 写入。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from feedkicker import bitable, feishu, store, wiki, wiki_home
from feedkicker.config import Config, load_config


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


NODES = [
    {"node_token": "tokB", "obj_type": "docx", "title": "话题B_2026-09-08_大纲"},
    {"node_token": "tokA", "obj_type": "docx", "title": "话题A_2026-09-08_大纲"},
    {"node_token": "tokC", "obj_type": "docx", "title": "话题C_2026-08-20_大纲"},
    {"obj_type": "docx", "title": "无token_2026-09-01_大纲"},
    {"node_token": "tokD", "obj_type": "sheet", "title": "表格_2026-09-01_大纲"},
    {"node_token": "tokE", "obj_type": "docx", "title": "普通文档标题"},
]

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=wiki.SHANGHAI)


def node_list_proc(nodes):
    return FakeProc(0, stdout=json.dumps({"ok": True, "data": {"nodes": nodes}}, ensure_ascii=False))


def install_fake_lark(monkeypatch, nodes, update_stdout="updated"):
    """拦截 bitable._run：+node-list 返回 nodes，+update 记录 argv 与内容文件。"""
    calls: list[list[str]] = []
    contents: dict[str, str] = {}

    def fake_run(args, stdin_text=None, timeout=120):
        calls.append(list(args))
        if "+node-list" in args:
            return node_list_proc(nodes)
        if "+update" in args:
            idx = args.index("--content")
            rel = args[idx + 1].lstrip("@")
            contents[rel] = Path(rel).read_text(encoding="utf-8")
            return FakeProc(0, stdout=update_stdout)
        return FakeProc(1, stderr=f"unexpected args: {args}")

    monkeypatch.setattr(bitable, "_run", fake_run)
    return calls, contents


# ── build_home_md / list_outline_docs 纯逻辑 ──


def test_list_outline_docs_filters_and_sorts(monkeypatch):
    monkeypatch.setattr(wiki_home.wiki_lark, "lark_node_list", lambda *a, **k: node_list_proc(NODES))
    docs = wiki_home.list_outline_docs("spc", "parent")
    assert [d["node_token"] for d in docs] == ["tokA", "tokB", "tokC"]
    assert docs[0]["date"] == "2026-09-08"
    assert docs[0]["topic"] == "话题A"
    assert docs[0]["url"] == wiki.wiki_url("tokA")


def test_build_home_md_monthly_blocks_newest_first():
    docs = [
        {"date": "2026-09-08", "topic": "话题A", "node_token": "tokA", "url": wiki.wiki_url("tokA")},
        {"date": "2026-09-08", "topic": "话题B", "node_token": "tokB", "url": wiki.wiki_url("tokB")},
        {"date": "2026-08-20", "topic": "话题C", "node_token": "tokC", "url": wiki.wiki_url("tokC")},
    ]
    md = wiki_home.build_home_md(docs, now=NOW)

    assert md.index("## 2026年9月") < md.index("## 2026年8月")
    assert "2026年09月" not in md
    assert "共 3 篇" in md
    assert md.count("| 生成日期 | 文件名 | 链接 |") == 2
    assert md.count("|---|---|---|") == 2

    sept = md.split("## 2026年8月")[0]
    rows = [ln for ln in sept.splitlines() if ln.startswith("| 2026-")]
    assert rows == [
        f"| 2026-09-08 | 话题A | [打开]({wiki.wiki_url('tokA')}) |",
        f"| 2026-09-08 | 话题B | [打开]({wiki.wiki_url('tokB')}) |",
    ]
    assert "| 2026-08-20 | 话题C |" in md


def test_build_home_md_topic_is_plain_text_not_link():
    docs = [{"date": "2026-09-08", "topic": "话题X", "node_token": "t", "url": wiki.wiki_url("t")}]
    md = wiki_home.build_home_md(docs, now=NOW)
    assert "话题X](http" not in md
    assert "[打开](" + wiki.wiki_url("t") in md


def test_build_home_md_empty():
    md = wiki_home.build_home_md([], now=NOW)
    assert "共 0 篇" in md
    assert "## " not in md


# ── update_homepage ──


def test_update_homepage_dry_run_prints_and_never_writes(monkeypatch, capsys):
    calls, _ = install_fake_lark(monkeypatch, NODES)
    assert wiki_home.update_homepage("spc", "parentNode", dry_run=True, now=NOW) is True
    assert not any("+update" in c for c in calls)
    out = capsys.readouterr().out
    assert "## 2026年9月" in out
    assert "话题A" in out
    assert list(Path(".").glob(".wiki-home-*.md")) == []


def test_update_homepage_apply_overwrites_parent_node(monkeypatch):
    calls, contents = install_fake_lark(monkeypatch, NODES)
    assert wiki_home.update_homepage("spc", "parentNode", dry_run=False, now=NOW) is True

    update_calls = [c for c in calls if "+update" in c]
    assert len(update_calls) == 1
    argv = update_calls[0]
    assert argv[:6] == ["docs", "+update", "--doc", "parentNode", "--command", "overwrite"]
    assert argv[argv.index("--doc-format") + 1] == "markdown"
    rel = argv[argv.index("--content") + 1]
    assert rel.startswith("@./.wiki-home-") and rel.endswith(".md")
    assert list(Path(".").glob(".wiki-home-*.md")) == []

    content = next(iter(contents.values()))
    assert "## 2026年9月" in content
    assert "[打开](" + wiki.wiki_url("tokA") in content


def test_update_homepage_node_list_failure_returns_false(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(args, stdin_text=None, timeout=120):
        calls.append(list(args))
        return FakeProc(1, stderr="boom")

    monkeypatch.setattr(bitable, "_run", fake_run)
    assert wiki_home.update_homepage("spc", "parent", dry_run=False) is False
    assert not any("+update" in c for c in calls)


def test_update_homepage_node_list_raises_returns_false(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("lark-cli missing")

    monkeypatch.setattr(wiki_home.wiki_lark, "lark_node_list", boom)
    assert wiki_home.update_homepage("spc", "parent", dry_run=False) is False


def test_update_homepage_overwrite_business_failure(monkeypatch):
    fail = json.dumps({"ok": False, "error": {"message": "no permission"}}, ensure_ascii=False)
    calls, _ = install_fake_lark(monkeypatch, NODES, update_stdout=fail)
    assert wiki_home.update_homepage("spc", "parent", dry_run=False) is False
    assert any("+update" in c for c in calls)


# ── CLI ──


def _stub_cfg():
    cfg = Config(app_env="test")
    cfg.wiki.space_id = "spc"
    cfg.wiki.parent_token = "par"
    return cfg


def test_cli_dry_run(monkeypatch, capsys):
    monkeypatch.setattr(wiki_home, "load_config", lambda *a, **k: _stub_cfg())
    monkeypatch.setattr(wiki_home.wiki_lark, "lark_node_list", lambda *a, **k: node_list_proc(NODES))
    rc = wiki_home.main(["--dry-run", "--env", "test"])
    assert rc == 0
    assert "## 2026年9月" in capsys.readouterr().out


def test_cli_missing_wiki_tokens_rc2(monkeypatch):
    monkeypatch.setattr(wiki_home, "load_config", lambda *a, **k: Config(app_env="test"))
    assert wiki_home.main(["--env", "test"]) == 2


# ── salon_flow 集成 ──


def _salon_cfg():
    cfg = load_config(app_env="test")
    cfg.salon.app_token = "app"
    cfg.salon.table_id = "tbl"
    cfg.salon.wiki_space_id = "spc_test"
    cfg.salon.wiki_parent_token = "parent_test"
    cfg.wiki.space_id = "spc_test"
    cfg.wiki.parent_token = "parent_test"
    cfg.wiki.app_token = "app_wiki"
    cfg.minimax.api_key = "sk-test"
    cfg.feishu_webhook = "https://hook.test"
    return cfg


def _stub_salon_deps(monkeypatch):
    from feedkicker import salon_flow as sf

    monkeypatch.setattr(sf, "fetch_selected_topics", lambda *a, **k: [
        {"record_id": "rec1", "fields": {"讨论状态": ["已选题"], "话题名称": "话题A"}},
    ])
    monkeypatch.setattr(
        sf.minimax, "gen_outline",
        lambda *a, **k: {"title": "t", "slides": [{"heading": "h", "bullets": ["a"]}]},
    )
    monkeypatch.setattr(
        sf.wiki, "create_wiki_doc_from_md",
        lambda *a, **k: "https://web91vfvm7.feishu.cn/wiki/wik1",
    )
    monkeypatch.setattr(feishu, "send", lambda *a, **k: True)
    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(sf.wiki_home, "update_homepage", lambda *a, **k: calls.append((a, k)) or True)
    return sf, calls


def test_salon_flow_apply_calls_update_homepage(monkeypatch):
    sf, calls = _stub_salon_deps(monkeypatch)
    conn = store.connect(":memory:")
    assert sf.run(_salon_cfg(), conn, dry_run=False) == 0
    assert calls, "salon_flow 应在卡片后更新 Wiki 首页"
    args, kwargs = calls[-1]
    assert args[0] == "spc_test"
    assert args[1] == "parent_test"
    assert kwargs.get("dry_run") is False
    conn.close()


def test_salon_flow_dry_run_calls_update_homepage_dry(monkeypatch):
    sf, calls = _stub_salon_deps(monkeypatch)
    conn = store.connect(":memory:")
    assert sf.run(_salon_cfg(), conn, dry_run=True) == 0
    assert calls, "dry-run 也应触发首页预览"
    assert calls[-1][1].get("dry_run") is True
    conn.close()
