"""第五轮审计 P1（#232/#233/#234）——先失败后修复的验收用例。

全部 lark-cli/httpx 经 monkeypatch 拦截：无真实子进程、无真实网络。
"""

from __future__ import annotations

import json
import logging

import pytest

from feedkicker import bitable_backfill, bitable_lark, bitable_purge, bitable_records, feishu, store
from feedkicker.config import load_config


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class PagedServer:
    """遵守 --offset 的假 lark-cli：总量可配置，页面互不重复。"""

    def __init__(self, total: int = 20300):
        self.total = total
        self.calls = 0

    def run(self, args, stdin_text=None, timeout=120):
        self.calls += 1
        if args[:2] == ["base", "--help"]:
            return FakeProc(0, "+record-batch-update")
        if "+record-list" not in args:
            return FakeProc(0, "{}")
        limit = int(args[args.index("--limit") + 1])
        offset = int(args[args.index("--offset") + 1])
        records = [
            {
                "record_id": f"recR{i:07d}",
                "fields": {
                    "链接": f"https://e.com/{i}",
                    "推送时间": "2026-01-01",
                    "归档日期": "2026-01-01",
                    "环境": "dev",
                },
            }
            for i in range(offset, min(offset + limit, self.total))
        ]
        return FakeProc(0, json.dumps({"data": {"records": records}}, ensure_ascii=False))


# ── #232 合法大表（20300 行）不得被误杀 ──


def test_existing_links_large_table_pages_through(monkeypatch):
    server = PagedServer(20300)
    monkeypatch.setattr(bitable_lark, "_run", server.run)
    links = bitable_records.existing_links("app", "tbl")
    assert len(links) == 20300
    assert server.calls == 20300 // 200 + 1


def test_list_records_large_table_pages_through(monkeypatch):
    server = PagedServer(20300)
    monkeypatch.setattr(bitable_lark, "_run", server.run)
    pairs, list_ok, complete = bitable_purge._list_records("app", "tbl")
    assert list_ok and complete
    assert len(pairs) == 20300


def test_backfill_large_table_pages_through(monkeypatch):
    server = PagedServer(20300)
    monkeypatch.setattr(bitable_lark, "_run", server.run)
    assert bitable_backfill.backfill_empty_archive_dates("app", "tbl") == 0
    assert server.calls >= 20300 // 200


# ── #232 忽略 --offset 恒返同页：页指纹必须尽少数迭代内命中 ──


def _same_page_run(calls: list[list[str]]):
    def fake_run(args, stdin_text=None, timeout=120):
        calls.append(list(args))
        if args[:2] == ["base", "--help"]:
            return FakeProc(0, "+record-batch-update")
        records = [
            {"record_id": f"recSame{i:03d}", "fields": {"链接": f"https://e.com/{i}", "环境": "dev"}}
            for i in range(200)
        ]
        return FakeProc(0, json.dumps({"data": {"records": records}}, ensure_ascii=False))

    return fake_run


def test_existing_links_repeated_page_fingerprint(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(bitable_lark, "_run", _same_page_run(calls))
    with pytest.raises(RuntimeError, match="分页|offset"):
        bitable_records.existing_links("app", "tbl")
    assert len(calls) <= 3, "页指纹需在第 2 页即命中，不得靠 offset 天花板"


def test_list_records_repeated_page_fingerprint(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(bitable_lark, "_run", _same_page_run(calls))
    with pytest.raises(RuntimeError, match="分页|offset"):
        bitable_purge._list_records("app", "tbl")
    assert len(calls) <= 3


def test_backfill_repeated_page_fingerprint(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(bitable_lark, "_run", _same_page_run(calls))
    with pytest.raises(RuntimeError, match="分页|offset"):
        bitable_backfill.backfill_empty_archive_dates("app", "tbl")
    assert len(calls) <= 4


# ── #232 backfill 异常不得冒 traceback ──


def test_bitable_backfill_error_returns_2(monkeypatch, tmp_path, caplog):
    from feedkicker import bitable
    from feedkicker.config import Config

    cfg = Config(app_env="test")
    cfg.bitable.enabled = True
    cfg.bitable.app_token = "appReal"
    cfg.bitable.table_id = "tblReal"
    cfg.db_path = str(tmp_path / "b5.sqlite3")
    monkeypatch.setattr("feedkicker.config.load_config", lambda *a, **kw: cfg)

    def boom(app_token, table_id, env_name=None, dry_run=False):
        raise RuntimeError("分页未前进（疑似 lark-cli 忽略 --offset）")

    monkeypatch.setattr(bitable_backfill, "backfill_empty_archive_dates", boom)
    monkeypatch.setattr(bitable_records, "sync_env", lambda *a, **k: 0)
    with caplog.at_level(logging.ERROR):
        assert bitable.main(["--backfill", "--env", "test"]) == 2
    assert any("backfill" in r.getMessage() for r in caplog.records)


# ── #233 多源 20KB 裁剪：丢全局最旧、留全局最新 ──


def _feed_items(feed: str, prefix: str, month: str) -> list[dict]:
    return [
        {
            "feed_id": feed,
            "entry_key": f"{prefix}{i}",
            "title": f"{prefix}{i} " + "标题填充" * 30,
            "url": f"https://e.com/{prefix}{i}",
            "description": "描述填充" * 80,
            "published_at": f"2026-{month}-{i:02d}T00:00:00Z",
        }
        for i in range(1, 6)
    ]


def _texts(card: dict) -> str:
    return "\n".join(
        e.get("text", {}).get("content", "")
        for e in card["card"]["elements"]
        if e.get("tag") == "div"
    )


def test_build_card_multisource_keeps_global_newest():
    items = _feed_items("B", "B", "01") + _feed_items("A", "A", "06")
    card = feishu.build_card(items, 0, ["A", "B"], max_bytes=3000)
    joined = _texts(card)
    assert "已截断" in joined
    assert "A5" in joined, "全局最新必须保留"
    assert "B1" not in joined, "全局最旧应先丢"
    assert "B5" in joined, "丢的是全局最旧，不是整源（A 组全保留后仍留 B 最新）"


def test_build_card_single_source_still_drops_oldest():
    items = _feed_items("F", "F", "01")
    card = feishu.build_card(items, 0, ["F"], max_bytes=1500)
    joined = _texts(card)
    assert "F5" in joined
    assert "F1" not in joined


# ── #234 salon 单条坏 LLM 响应不得拖垮整批 ──


def _salon_cfg(monkeypatch):
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
    return cfg


def test_salon_flow_bad_outline_isolated_per_topic(monkeypatch, caplog):
    from feedkicker import salon_flow as sf

    cfg = _salon_cfg(monkeypatch)
    conn = store.connect(":memory:")
    monkeypatch.setattr(sf, "fetch_selected_topics", lambda *a, **k: [
        {"record_id": "recBad", "fields": {"讨论状态": ["已选题"], "话题名称": "坏题"}},
        {"record_id": "recGood", "fields": {"讨论状态": ["已选题"], "话题名称": "好题"}},
    ])

    def fake_gen(topic, kind="tool", api_key=None, base_url=None, model=None):
        if topic == "坏题":
            return {"title": "t", "slides": "oops"}
        return {"title": f"{kind}-{topic}", "slides": [{"heading": "h", "bullets": ["a"]}]}

    monkeypatch.setattr(sf.minimax, "gen_outline", fake_gen)
    wiki_calls: list[str] = []
    monkeypatch.setattr(
        sf.wiki,
        "create_wiki_doc_from_md",
        lambda app, space, parent, title, md, dry_run=False, date_str=None: (
            wiki_calls.append(title) or f"https://x/wiki/{title}"
        ),
    )
    sent: list[list[str]] = []
    monkeypatch.setattr(feishu, "send", lambda payload, *a, **kw: sent.append(list(kw.values())) or True)
    with caplog.at_level(logging.WARNING):
        rc = sf.run(cfg, conn, dry_run=False)
    assert rc == 0, "单题坏响应不得改变返回码"
    assert wiki_calls == ["好题"], "正常题必须建 Wiki，坏题跳过"
    assert len(sent) == 1, "卡片必须照发（含正常题链接）"
    assert store.get_ppt_last_status(conn, "recGood") == "已选题"
    assert store.get_ppt_last_status(conn, "recBad") == ""
    assert any("坏" in r.getMessage() or "大纲" in r.getMessage() for r in caplog.records)
    conn.close()


def test_outline_to_md_type_normalization():
    from feedkicker import salon_md

    md = salon_md.outline_to_md(
        {
            "title": "T",
            "slides": [
                {"heading": "h1", "bullets": "not-a-list", "speaker_note": 42},
                "not-a-dict",
                {"heading": "h2", "bullets": ["ok", None, 7], "speaker_note": "note"},
            ],
        },
        "工具类大纲",
    )
    rendered = md.split("```json")[0]
    assert "h1" in rendered and "h2" in rendered and "note" in rendered
    assert "not-a-dict" not in rendered

    with pytest.raises(ValueError, match="slides"):
        salon_md.outline_to_md({"title": "T", "slides": "oops"}, "工具类大纲")
