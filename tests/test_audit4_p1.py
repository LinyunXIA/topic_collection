"""第四轮审计 P1（#218 / #219）。

#218：reseed 先 reset 后按「环境」清行（dev/test 共享 Base 保留另一环境），
purge 失败必须中止（rc 2）不重灌；#219：wiki/salon 空或占位 parent 守卫。
全部 lark-cli/httpx 经 monkeypatch 拦截，无真实子进程、无真实网络。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from feedkicker import bitable, bitable_lark, bitable_records, store, wiki
from feedkicker.config import Config, WikiConf, load_config


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeBaseServer:
    """最小 lark-cli base 行为：record-list / record-delete / record-batch-create 全在内存。"""

    def __init__(self, rows: list[dict]):
        self.rows = [dict(r) for r in rows]
        self.calls: list[list[str]] = []

    def run(self, args, stdin_text=None, timeout=120):
        self.calls.append(list(args))
        if "+record-list" in args:
            records = [
                {"record_id": r["record_id"], "fields": {"环境": r["env"], "链接": r["link"]}}
                for r in self.rows
            ]
            return FakeProc(0, json.dumps({"data": {"records": records}}, ensure_ascii=False))
        if "+record-delete" in args:
            payload = json.loads(args[args.index("--json") + 1])
            ids = set(payload["record_id_list"])
            self.rows = [r for r in self.rows if r["record_id"] not in ids]
            return FakeProc(0, "{}")
        if "+record-batch-create" in args:
            raw = args[args.index("--json") + 1]
            payload = json.loads(Path(raw[1:]).read_text(encoding="utf-8"))
            for rec in payload["create_records"]:
                self.rows.append(
                    {
                        "record_id": f"recNew{len(self.rows)}",
                        "env": rec.get("环境"),
                        "link": rec.get("链接"),
                    }
                )
            return FakeProc(0, "{}")
        return FakeProc(0, "{}")


def _cfg(tmp_path, app_env="dev") -> Config:
    cfg = Config(app_env=app_env)
    cfg.bitable.enabled = True
    cfg.bitable.app_token = "appReal"
    cfg.bitable.table_id = "tblReal"
    cfg.db_path = str(tmp_path / "t.sqlite3")
    return cfg


def _seed_conn(cfg) -> None:
    conn = store.connect(cfg.db_path)
    store.download(
        conn,
        "F",
        [{"entry_key": "kDev", "title": "dev 资讯", "url": "https://e.com/dev-new", "description": "", "published_at": None}],
        "2026-09-14T00:00:00Z",
    )
    conn.execute("UPDATE articles SET bitable_synced_at = ?", ("2026-09-14T01:00:00Z",))
    conn.commit()
    store.mark_topic_archived(conn, "tbl", "recSalon", "沙龙选题", "https://e.com/salon", "摘录", "2026-09-14T02:00:00Z")
    conn.close()


# ── #218 reseed 数据安全 ──


def test_reseed_dev_keeps_test_rows_and_reloads_dev(monkeypatch, tmp_path):
    """dev reseed 只清「环境」=dev 行，test 行原样保留；dev 行清后重灌。"""
    server = FakeBaseServer(
        [
            {"record_id": "recDevOld", "env": "dev", "link": "https://e.com/dev-old"},
            {"record_id": "recTestKeep", "env": "test", "link": "https://e.com/test-keep"},
        ]
    )
    cfg = _cfg(tmp_path, app_env="dev")
    monkeypatch.setattr("feedkicker.config.load_config", lambda *a, **kw: cfg)
    monkeypatch.setattr(bitable_lark, "_run", server.run)
    _seed_conn(cfg)

    assert bitable.main(["--reseed", "--env", "dev"]) == 0

    by_id = {r["record_id"]: r for r in server.rows}
    assert "recTestKeep" in by_id, "test 环境行必须在 dev reseed 后保留（跨环境不清）"
    assert by_id["recTestKeep"]["link"] == "https://e.com/test-keep"
    assert "recDevOld" not in by_id, "dev 旧行应被清空"
    dev_rows = [r for r in server.rows if r["env"] == "dev"]
    assert len(dev_rows) == 1
    assert dev_rows[0]["link"] == "https://e.com/dev-new"
    assert all(r["link"] != "https://e.com/salon" for r in server.rows), "salon 占位行不重灌"


def test_reseed_aborts_when_purge_batch_fails(monkeypatch, tmp_path, caplog):
    """批删失败：rc 2、不得继续重灌；reset 先行（标记已清，下轮自愈）。"""

    def fake_run(args, stdin_text=None, timeout=120):
        if "+record-list" in args:
            records = [{"record_id": "recDevOld", "fields": {"环境": "dev", "链接": "https://e.com/dev-old"}}]
            return FakeProc(0, json.dumps({"data": {"records": records}}, ensure_ascii=False))
        if "+record-delete" in args:
            return FakeProc(1, stdout="", stderr="boom")
        if "+record-batch-create" in args:
            raise AssertionError("purge 未清净不得继续重灌")
        return FakeProc(0, "{}")

    cfg = _cfg(tmp_path, app_env="dev")
    monkeypatch.setattr("feedkicker.config.load_config", lambda *a, **kw: cfg)
    monkeypatch.setattr(bitable_lark, "_run", fake_run)
    monkeypatch.setattr(
        bitable_records,
        "sync_env",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("purge 失败不得 sync")),
    )
    _seed_conn(cfg)

    with caplog.at_level(logging.ERROR):
        assert bitable.main(["--reseed", "--env", "dev"]) == 2
    assert any("清理未完成" in r.getMessage() for r in caplog.records)

    conn = store.connect(cfg.db_path)
    unsynced = [r["entry_key"] for r in store.select_unsynced(conn)]
    conn.close()
    assert unsynced == ["kDev"], "reset 先行：标记已清，下轮可自愈重灌"


def test_reseed_resets_before_purge(monkeypatch, tmp_path):
    """顺序不变量：purge 调用时 bitable_synced_at 必须已全部清空（先 reset）。"""
    cfg = _cfg(tmp_path, app_env="dev")
    monkeypatch.setattr("feedkicker.config.load_config", lambda *a, **kw: cfg)
    _seed_conn(cfg)

    observed: dict[str, object] = {}

    def spy_purge(app_token, table_id, dry_run=False, env_name=None):
        probe = store.connect(cfg.db_path)
        observed["synced"] = probe.execute(
            "SELECT COUNT(*) FROM articles WHERE bitable_synced_at IS NOT NULL"
        ).fetchone()[0]
        probe.close()
        observed["env_name"] = env_name
        return 0, True

    monkeypatch.setattr(bitable_records, "purge_all_records", spy_purge)
    monkeypatch.setattr(bitable_records, "sync_env", lambda *a, **k: 0)
    assert bitable.main(["--reseed", "--env", "dev"]) == 0
    assert observed == {"synced": 0, "env_name": "dev"}


# ── #219 wiki parent 守卫 ──


def test_create_wiki_doc_requires_space_and_parent(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(bitable_lark, "_run", lambda *a, **k: calls.append(a) or FakeProc(0, "{}"))
    with pytest.raises(RuntimeError, match="space_id|parent"):
        wiki.create_wiki_doc_from_md("app", "", "parent", "T", "# md")
    with pytest.raises(RuntimeError, match="space_id|parent"):
        wiki.create_wiki_doc_from_md("app", "spc", "", "T", "# md")
    with pytest.raises(RuntimeError, match="space_id|parent"):
        wiki.create_wiki_doc_from_md("app", "<spc>", "parent", "T", "# md")
    assert calls == [], "守卫必须在任何 lark-cli 调用之前"


def test_wiki_cli_any_missing_token_loads_config(monkeypatch):
    """只缺 --parent-token 也必须触发配置回退（历史 bug：被 app/space 门住）。"""
    cfg = Config(app_env="test")
    cfg.wiki = WikiConf()
    cfg.wiki.app_token = "cfgApp"
    cfg.wiki.space_id = "cfgSpace"
    cfg.wiki.parent_token = "cfgParent"
    loaded: list[int] = []
    monkeypatch.setattr("feedkicker.config.load_config", lambda *a, **kw: loaded.append(1) or cfg)
    calls: list[list[str]] = []

    def fake_run(args, stdin_text=None, timeout=120):
        calls.append(list(args))
        if args[:2] == ["docs", "+create"]:
            return FakeProc(0, json.dumps({"ok": True, "data": {"document": {"document_id": "d1"}}}))
        if args[:2] == ["wiki", "+node-get"]:
            return FakeProc(0, json.dumps({"ok": True, "data": {"node_token": "n1", "obj_type": "docx"}}))
        return FakeProc(0, "{}")

    monkeypatch.setattr(bitable_lark, "_run", fake_run)
    rc = wiki.main(["--app-token", "A", "--space-id", "S", "--title", "T", "--env", "test"])
    assert rc == 0
    assert loaded, "缺 parent 时必须加载配置补全"
    create = [c for c in calls if c[:2] == ["docs", "+create"]][0]
    assert create[create.index("--parent-token") + 1] == "cfgParent"


def test_wiki_cli_missing_parent_returns_2_without_lark(monkeypatch, caplog):
    cfg = Config(app_env="test")
    cfg.wiki = WikiConf()
    monkeypatch.setattr("feedkicker.config.load_config", lambda *a, **kw: cfg)
    calls: list[tuple] = []
    monkeypatch.setattr(bitable_lark, "_run", lambda *a, **k: calls.append(a) or FakeProc(0, "{}"))
    with caplog.at_level(logging.ERROR):
        assert wiki.main(["--app-token", "A", "--space-id", "S", "--env", "test"]) == 2
    assert calls == [], "空 parent 不得调用 lark-cli"
    assert any("parent" in r.getMessage() for r in caplog.records)


def _salon_cfg(monkeypatch):
    monkeypatch.delenv("MiniMax_Key", raising=False)
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    cfg = load_config(app_env="test")
    cfg.salon.app_token = "appTokenTest"
    cfg.salon.table_id = "tblTest"
    cfg.salon.wiki_space_id = ""
    cfg.salon.wiki_parent_token = ""
    cfg.wiki = WikiConf()
    cfg.minimax.api_key = "sk-test"
    cfg.feishu_webhook = "https://hook.test"
    return cfg


def test_salon_flow_missing_wiki_token_skips_create(monkeypatch):
    from feedkicker import feishu
    from feedkicker import salon_flow as sf

    cfg = _salon_cfg(monkeypatch)
    conn = store.connect(":memory:")
    monkeypatch.setattr(sf, "fetch_selected_topics", lambda *a, **k: [
        {"record_id": "rec1", "fields": {"讨论状态": ["已选题"], "话题名称": "T1"}},
    ])
    wiki_calls: list[tuple] = []
    llm_calls: list[str] = []
    monkeypatch.setattr(sf.minimax, "gen_outline", lambda *a, **k: llm_calls.append("x") or {"title": "t", "slides": []})
    monkeypatch.setattr(sf.wiki, "create_wiki_doc_from_md", lambda *a, **k: wiki_calls.append(a) or "https://x/wiki/1")
    monkeypatch.setattr(feishu, "send", lambda *a, **k: (_ for _ in ()).throw(AssertionError("无链接不推卡")))

    assert sf.run(cfg, conn, dry_run=False) == 0
    assert wiki_calls == [], "空 wiki space/parent 不得建文档"
    assert llm_calls == [], "跳过建 wiki 时不得白调 LLM"
    assert store.get_ppt_last_status(conn, "rec1") == ""
    row = conn.execute("SELECT ppt_synced_at FROM articles WHERE entry_key='rec1'").fetchone()
    assert row is None
    conn.close()


def test_salon_flow_placeholder_wiki_token_skips_create(monkeypatch):
    from feedkicker import salon_flow as sf

    cfg = _salon_cfg(monkeypatch)
    cfg.wiki.space_id = "<wiki-space-id>"
    cfg.wiki.parent_token = "<node_token>"
    conn = store.connect(":memory:")
    monkeypatch.setattr(sf, "fetch_selected_topics", lambda *a, **k: [
        {"record_id": "rec1", "fields": {"讨论状态": ["已选题"], "话题名称": "T1"}},
    ])
    wiki_calls: list[tuple] = []
    monkeypatch.setattr(sf.wiki, "create_wiki_doc_from_md", lambda *a, **k: wiki_calls.append(a) or "https://x/wiki/1")
    monkeypatch.setattr(sf.minimax, "gen_outline", lambda *a, **k: {"title": "t", "slides": []})

    assert sf.run(cfg, conn, dry_run=False) == 0
    assert wiki_calls == [], "占位 wiki token 不得建文档"
    assert store.get_ppt_last_status(conn, "rec1") == ""
    conn.close()
