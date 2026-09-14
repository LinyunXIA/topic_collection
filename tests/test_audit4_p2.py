"""第四轮审计 P2（#220/#221/#222/#223/#224/#226）。

全部 lark-cli/httpx 经 monkeypatch 拦截，无真实子进程、无真实网络。
"""

from __future__ import annotations

import json

import pytest

from feedkicker import (
    bitable,
    bitable_backfill,
    bitable_lark,
    bitable_purge,
    bitable_records,
    feishu,
    store,
    wiki_home,
)
from feedkicker.config import Config, load_config

SIGN_LIMIT = 20000 - feishu.SIGN_RESERVE_BYTES


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ── #220 select_pending first_seen ──


def test_select_pending_carries_first_seen_and_top_n_keeps_latest():
    conn = store.connect(":memory:")
    for i in range(8):
        store.download(
            conn,
            "F",
            [{"entry_key": f"k{i}", "title": f"T{i}", "url": f"https://e.com/{i}", "description": "", "published_at": None}],
            f"2026-09-{i + 1:02d}T00:00:00Z",
        )
    pending = store.select_pending(conn)
    conn.close()
    assert all("first_seen" in r for r in pending), "select_pending 必须返回 first_seen 供 top_n 时效键使用"
    card = feishu.build_card(pending, 0, ["F"], top_n=3)
    content = card["card"]["elements"][0]["text"]["content"]
    assert all(f"T{i}" in content for i in (5, 6, 7)), "top_n 必须保留最新 3 条"
    assert all(f"T{i}" not in content for i in range(5)), "不得退化为保最旧"


# ── #221 占位行 mark 失败也不泄漏 ──


def test_mark_topic_archived_inserts_ppt_synced_at_directly(monkeypatch):
    from feedkicker import store_salon

    conn = store.connect(":memory:")
    monkeypatch.setattr(
        store_salon,
        "mark_ppt_synced",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db boom")),
    )
    store.mark_topic_archived(conn, "tbl", "recStub000", "选题", "https://e.com/s", "摘录", "2026-09-14T02:00:00Z")
    assert store.select_pending(conn) == [], "mark 失败时占位行仍不得进 push 选择"
    assert store.select_unsynced(conn) == [], "mark 失败时占位行仍不得进归档选择"
    row = conn.execute("SELECT ppt_synced_at FROM articles WHERE entry_key='recStub000'").fetchone()
    assert row is not None and row[0] == "2026-09-14T02:00:00Z"
    conn.close()


# ── #222 wiki_urls 截断 + 签名余量 ──


def _long_urls(n: int) -> list[str]:
    return [f"https://web91vfvm7.feishu.cn/wiki/wik{'a' * 220}_{i}" for i in range(n)]


def _div_texts(card) -> str:
    return " ".join(
        e.get("text", {}).get("content", "")
        for e in card["card"]["elements"]
        if e.get("tag") == "div"
    )


def test_build_card_drops_wiki_urls_with_hint_when_over_limit():
    urls = _long_urls(40)
    card = feishu.build_card([], 0, [], max_bytes=SIGN_LIMIT, wiki_urls=urls)
    raw = json.dumps(card, ensure_ascii=False).encode("utf-8")
    assert len(raw) <= SIGN_LIMIT
    actions = [e for e in card["card"]["elements"] if e.get("tag") == "action"]
    assert 0 < len(actions) < 40, "超限必须从尾部丢 wiki 链接"
    assert "已截断" in _div_texts(card), "截断需在卡片提示"


def test_salon_notify_payload_fits_with_sign_reserve(monkeypatch):
    from feedkicker import salon_notify

    cfg = load_config(app_env="test")
    cfg.feishu_webhook = "https://hook.test"
    cfg.app_env = "test"
    conn = store.connect(":memory:")
    sent: dict = {}

    def fake_send(payload, *a, **kw):
        sent["payload"] = payload
        return True

    monkeypatch.setattr(salon_notify.feishu, "send", fake_send)
    assert salon_notify.send_wiki_card(cfg, conn, _long_urls(40)) is True
    body = json.dumps(sent["payload"], ensure_ascii=False).encode("utf-8")
    assert len(body) + feishu.SIGN_RESERVE_BYTES <= 20000, "salon 卡未留签名余量"
    conn.close()


# ── #223 offset 上限守卫 ──


def test_existing_links_offset_guard(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(args, stdin_text=None, timeout=120):
        calls.append(list(args))
        records = [{"record_id": f"r{i}", "fields": {"链接": f"https://e.com/{i}"}} for i in range(200)]
        return FakeProc(0, json.dumps({"data": {"records": records}}))

    monkeypatch.setattr(bitable_lark, "_run", fake_run)
    with pytest.raises(RuntimeError, match="offset|分页"):
        bitable_records.existing_links("app", "tbl")
    assert len(calls) < 120, "必须在 offset 上限内终止，不得死循环"


def test_list_records_offset_guard(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(args, stdin_text=None, timeout=120):
        calls.append(list(args))
        records = [
            {"record_id": f"r{i}", "fields": {"环境": "dev", "推送时间": "2026-01-01", "归档日期": ""}}
            for i in range(200)
        ]
        return FakeProc(0, json.dumps({"data": {"records": records}}))

    monkeypatch.setattr(bitable_lark, "_run", fake_run)
    with pytest.raises(RuntimeError, match="offset|分页"):
        bitable_purge._list_records("app", "tbl")
    assert len(calls) < 120


def test_backfill_offset_guard(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(args, stdin_text=None, timeout=120):
        calls.append(list(args))
        if "+record-list" in args:
            records = [
                {"record_id": f"r{i}", "fields": {"推送时间": "2026-01-01", "归档日期": ""}}
                for i in range(200)
            ]
            return FakeProc(0, json.dumps({"data": {"records": records}}))
        return FakeProc(0, stdout="+record-batch-update")

    monkeypatch.setattr(bitable_lark, "_run", fake_run)
    with pytest.raises(RuntimeError, match="offset|分页"):
        bitable_backfill.backfill_empty_archive_dates("app", "tbl")
    assert len(calls) < 120


# ── #224 非 --init/--reseed 且 token 缺失不得自动建 Base ──


@pytest.mark.parametrize("extra", [[], ["--backfill"]])
def test_bitable_requires_explicit_flag_without_tokens(monkeypatch, tmp_path, extra):
    cfg = Config(app_env="test")
    cfg.bitable.enabled = True
    cfg.bitable.app_token = ""
    cfg.bitable.table_id = ""
    cfg.db_path = str(tmp_path / "b.sqlite3")
    monkeypatch.setattr("feedkicker.config.load_config", lambda *a, **kw: cfg)
    calls: list[list[str]] = []
    monkeypatch.setattr(bitable_lark, "_run", lambda *a, **k: calls.append(list(a)) or FakeProc(0, "{}"))

    assert bitable.main([*extra, "--env", "test"]) == 2
    assert not any("+base-create" in c for c in calls)


# ── #226 空 node-list 不得覆写首页 ──


@pytest.mark.parametrize("stdout", [json.dumps({"ok": True, "data": {"nodes": []}}), "not-json-update-output"])
def test_update_homepage_empty_nodes_no_overwrite(monkeypatch, stdout, caplog):
    calls: list[list[str]] = []

    def fake_run(args, stdin_text=None, timeout=120):
        calls.append(list(args))
        return FakeProc(0, stdout=stdout)

    monkeypatch.setattr(bitable_lark, "_run", fake_run)
    assert wiki_home.update_homepage("spc", "parent", dry_run=False) is False
    assert not any("+update" in c for c in calls), "空索引不得 overwrite 首页"
