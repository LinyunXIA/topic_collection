"""第五轮审计 P3（#237/#238/#239）——边界/配置/并发。

全部 lark-cli/httpx 经 monkeypatch 拦截；docs 拓扑断言只读源码与文档。
"""

from __future__ import annotations

import json
import logging
import sqlite3

import pytest

from feedkicker import bitable_lark, store, wiki, wiki_home
from feedkicker import topic as topic_mod
from feedkicker.config import PROJECT_ROOT, Config, WikiConf, load_config


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


# ── #237(a)/#244 topic 容器校验：坏容器必须 raise（重扫判定「空页 + WARNING」吞错） ──


def test_topic_extract_records_container_validation():
    for bad in (
        "not-a-dict",
        ["x"],
        {"records": "ab"},
        {"records": {"x": 1}},
        {"records": [{"record_id": "r1"}, "bad"]},
        {"data": 1},
        {"x": 1},
    ):
        with pytest.raises(RuntimeError):
            topic_mod._extract_records(bad)
    # #326 收紧：顶层 dict 无可识别容器键（如 {"x":1}）不再算「真空页」，必须 raise
    assert topic_mod._extract_records({"records": []}) == []


def test_topic_extract_records_valid_shapes_still_work():
    assert topic_mod._extract_records({"records": [{"record_id": "r1"}]}) == [{"record_id": "r1"}]
    rows = topic_mod._extract_records(
        {"fields": ["话题名称"], "data": [{"record_id": "r2", "话题名称": "T"}]}
    )
    assert rows[0]["record_id"] == "r2"


def test_fetch_selected_topics_bad_container_raises(monkeypatch):
    monkeypatch.setattr(
        topic_mod.bitable_lark,
        "_run",
        lambda *a, **k: FakeProc(0, json.dumps({"data": {"records": "oops"}})),
    )
    with pytest.raises(RuntimeError, match="records"):
        topic_mod.fetch_selected_topics("app", "tbl")


# ── #237(b) salon 稳态全跳过不得误报「全部失败」 ──


def _salon_cfg(monkeypatch):
    from feedkicker import salon_flow as sf

    monkeypatch.delenv("MiniMax_Key", raising=False)
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    cfg = load_config(app_env="test")
    cfg.salon.enabled = True
    cfg.salon.app_token = "appTokenTest"
    cfg.salon.table_id = "tblTest"
    cfg.salon.wiki_space_id = "spc_test"
    cfg.salon.wiki_parent_token = "parent_test"
    cfg.wiki.space_id = "spc_test"
    cfg.wiki.parent_token = "parent_test"
    cfg.wiki.app_token = "app_wiki"
    cfg.minimax.api_key = "sk-test"
    cfg.feishu_webhook = "https://hook.test"
    monkeypatch.setattr(sf.wiki_home, "update_homepage", lambda *a, **k: True)
    return cfg


def test_salon_flow_steady_state_no_failure_warning(monkeypatch, caplog):
    from feedkicker import salon_flow as sf

    cfg = _salon_cfg(monkeypatch)
    conn = store.connect(":memory:")
    conn.execute(
        "INSERT INTO articles (feed_id, entry_key, title, url, description, published_at,"
        " first_seen, pushed_at, ppt_synced_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("tblTest", "recDone", "旧", "https://x", "d", None, "2026-09-01T00:00:00Z", None,
         "2026-09-02T00:00:00Z"),
    )
    conn.commit()
    store.set_ppt_last_status(conn, "recDone", "已选题")
    monkeypatch.setattr(sf, "fetch_selected_topics", lambda *a, **k: [
        {"record_id": "recDone", "fields": {"讨论状态": ["已选题"], "话题名称": "T"}},
    ])

    def boom(*a, **k):
        raise AssertionError("稳态全跳过不得调用 LLM")

    monkeypatch.setattr(sf.minimax, "gen_outline", boom)
    from feedkicker import feishu
    monkeypatch.setattr(feishu, "send", lambda *a, **k: True)
    with caplog.at_level(logging.WARNING):
        assert sf.run(cfg, conn, dry_run=False) == 0
    assert not any("全部失败" in r.getMessage() for r in caplog.records), "稳态全跳过不得误报"
    conn.close()


def test_salon_flow_attempted_all_failed_still_warns(monkeypatch, caplog):
    from feedkicker import salon_flow as sf

    cfg = _salon_cfg(monkeypatch)
    conn = store.connect(":memory:")
    monkeypatch.setattr(sf, "fetch_selected_topics", lambda *a, **k: [
        {"record_id": "rec1", "fields": {"讨论状态": ["已选题"], "话题名称": "T1"}},
    ])
    monkeypatch.setattr(
        sf.minimax, "gen_outline", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("429"))
    )
    from feedkicker import feishu
    monkeypatch.setattr(feishu, "send", lambda *a, **k: True)
    with caplog.at_level(logging.WARNING):
        assert sf.run(cfg, conn, dry_run=False) == 1
    assert any("全部失败" in r.getMessage() for r in caplog.records)
    conn.close()


# ── #237(c) 缺 lark-cli：wiki.main 必须 rc2 不冒 traceback ──


def test_wiki_cli_missing_lark_cli_returns_2(monkeypatch, caplog):
    def no_lark():
        raise FileNotFoundError("找不到 lark-cli")

    monkeypatch.setattr(bitable_lark, "lark_bin", no_lark)
    with caplog.at_level(logging.ERROR):
        rc = wiki.main(
            ["--app-token", "a", "--space-id", "s", "--parent-token", "p", "--title", "T", "--env", "test"]
        )
    assert rc == 2
    assert caplog.records, "缺 lark-cli 需有错误日志（不得静默）"


# ── #237(d) wiki_home 占位 space/parent → rc2、零 lark 调用 ──


@pytest.mark.parametrize(
    ("space", "parent"),
    [("", "parent"), ("spc", ""), ("<wiki-space-id>", "parent"), ("spc", "<node_token>")],
)
def test_wiki_home_main_placeholder_guard_rc2(monkeypatch, space, parent, caplog):
    cfg = Config(app_env="test")
    cfg.wiki = WikiConf()
    cfg.wiki.space_id = space
    cfg.wiki.parent_token = parent
    monkeypatch.setattr(wiki_home, "load_config", lambda *a, **kw: cfg)
    calls: list[tuple] = []
    monkeypatch.setattr(bitable_lark, "_run", lambda *a, **k: calls.append(a) or FakeProc(0, "{}"))
    with caplog.at_level(logging.ERROR):
        assert wiki_home.main(["--env", "test"]) == 2
    assert calls == [], "占位/空配置不得发只读 node-list"
    assert any("占位" in r.getMessage() or "未配置" in r.getMessage() for r in caplog.records)


# ── #237(e) minimax 成功码 "0" 归一 ──


def test_minimax_base_resp_string_zero_is_success(monkeypatch):
    import feedkicker.minimax as mm

    data = {
        "base_resp": {"status_code": "0"},
        "choices": [{"message": {"content": '{"title": "t", "slides": []}'}}],
    }
    monkeypatch.setattr(mm.httpx, "post", lambda *a, **k: FakeResp(200, data))
    out = mm.gen_outline("t", kind="tool", api_key="sk-test")
    assert out["title"] == "t"


# ── #238 docs 拓扑 ──


def test_docs_register_new_submodules():
    design = (PROJECT_ROOT / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    prd = (PROJECT_ROOT / "docs" / "PRD.md").read_text(encoding="utf-8")
    agents = (PROJECT_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    for name in ("bitable_reseed.py", "feishu_card_body.py", "feishu_host.py", "store_conn.py", "topic_records.py"):
        assert name in design, f"DESIGN 未登记 {name}"
        assert name in prd, f"PRD §6 未登记 {name}"
    assert "reseed" in agents, "AGENTS 子模块列表未补 bitable_reseed"
    ops = (PROJECT_ROOT / "docs" / "OPS.md").read_text(encoding="utf-8")
    assert "TC_FEISHU_HOST" in ops, "OPS 未文档化 TC_FEISHU_HOST"
    cli = (PROJECT_ROOT / "docs" / "CLI.md").read_text(encoding="utf-8")
    wiki_sec = cli.split("\n## feedkicker.wiki\n")[1].split("\n## feedkicker.topic")[0]
    assert "配置加载异常未捕获" not in wiki_sec, "CLI.md wiki 退出码措辞未更正"
    assert "token 守卫" in wiki_sec


# ── #239(a) feishu 占位清空 ──


def test_load_config_clears_feishu_placeholders(tmp_path):
    path = tmp_path / "cfg.yaml"
    path.write_text(
        'feishu_webhook: "<webhook>"\nfeishu_secret: "<prod-secret>"\nfeeds: []\n',
        encoding="utf-8",
    )
    cfg = load_config(config_path=path, app_env="test")
    assert cfg.feishu_webhook == ""
    assert cfg.feishu_secret == ""


def test_load_config_env_feishu_not_cleared(monkeypatch, tmp_path):
    path = tmp_path / "cfg.yaml"
    path.write_text('feishu_webhook: "<webhook>"\nfeeds: []\n', encoding="utf-8")
    monkeypatch.setenv("FEISHU_WEBHOOK", "https://hook.real/x")
    cfg = load_config(config_path=path, app_env="test")
    assert cfg.feishu_webhook == "https://hook.real/x"


# ── #239(b) store 并发：busy_timeout + WAL + 迁移容忍 duplicate ──


def test_store_connect_sets_busy_timeout_and_wal(tmp_path):
    conn = store.connect(tmp_path / "t.sqlite3")
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    conn.close()


def test_store_migration_tolerates_duplicate_column():
    from feedkicker import store_conn

    conn = sqlite3.connect(":memory:")
    conn.executescript(store_conn._SCHEMA)
    conn.execute("ALTER TABLE articles ADD COLUMN bitable_synced_at TEXT")
    conn.execute("ALTER TABLE articles ADD COLUMN ppt_synced_at TEXT")
    store_conn._add_columns(conn, set())
    conn.close()
