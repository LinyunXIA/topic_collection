"""第五轮重扫 P3（R5V-4..7 / #245）——失败优先验收用例。

全部 lark-cli/httpx 经 monkeypatch 拦截：无真实子进程、无真实网络。
"""

from __future__ import annotations

import json
import logging

import pytest

from feedkicker import (
    bitable,
    bitable_backfill,
    bitable_lark,
    bitable_records,
    bitable_schema,
    minimax,
)
from feedkicker import topic as topic_mod
from feedkicker.config import Config


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ── R5V-4 绝对兜底 = _CHUNK × _MAX_PAGES，可推导且不误杀边界 ──


def test_max_offset_last_derived_from_chunk_and_pages():
    assert bitable_lark.MAX_OFFSET_LAST == bitable_lark._CHUNK * bitable_lark._MAX_PAGES
    assert bitable_lark.MAX_OFFSET_LAST == 200000


def test_guard_offset_allows_last_and_rejects_beyond():
    bitable_lark._guard_offset(bitable_lark.MAX_OFFSET_LAST)
    with pytest.raises(RuntimeError, match="兜底"):
        bitable_lark._guard_offset(bitable_lark.MAX_OFFSET_LAST + bitable_lark._CHUNK)


# ── R5V-5 topic 与 bitable 路径统一：页指纹 + 20 万兜底 ──


def test_topic_ignored_offset_same_page_raises_on_second_page(monkeypatch):
    calls = {"n": 0}
    page = {"records": [{"record_id": "recSame", "fields": {}}], "has_more": True}

    def fake_run(args, stdin_text=None, timeout=120):
        calls["n"] += 1
        return FakeProc(0, json.dumps({"data": page}, ensure_ascii=False))

    monkeypatch.setattr(topic_mod.bitable_lark, "_run", fake_run)
    with pytest.raises(RuntimeError, match="分页|offset"):
        topic_mod.fetch_selected_topics("app", "tbl")
    assert calls["n"] == 2, "同页指纹必须第 2 页熔断"


def test_topic_large_table_pages_through(monkeypatch):
    total = 20300

    def fake_run(args, stdin_text=None, timeout=120):
        limit = int(args[args.index("--limit") + 1])
        offset = int(args[args.index("--offset") + 1])
        records = [{"record_id": f"recR{i:06d}"} for i in range(offset, min(offset + limit, total))]
        payload = {"records": records, "has_more": offset + limit < total}
        return FakeProc(0, json.dumps({"data": payload}, ensure_ascii=False))

    monkeypatch.setattr(topic_mod.bitable_lark, "_run", fake_run)
    assert len(topic_mod.fetch_selected_topics("app", "tbl")) == total


# ── R5V-6 缺 lark-cli 不得静默成功 ──


def test_backfill_missing_batch_verb_raises(monkeypatch):
    monkeypatch.setattr(bitable_lark, "_has_batch_verb", lambda: None)
    with pytest.raises(RuntimeError, match="lark-cli"):
        bitable_backfill.backfill_empty_archive_dates("app", "tbl")


def test_backfill_run_none_raises(monkeypatch):
    monkeypatch.setattr(bitable_lark, "_run", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="lark-cli"):
        bitable_backfill.backfill_empty_archive_dates("app", "tbl")


def _main_cfg(tmp_path) -> Config:
    cfg = Config(app_env="test")
    cfg.bitable.enabled = True
    cfg.bitable.app_token = "appReal"
    cfg.bitable.table_id = "tblReal"
    cfg.db_path = str(tmp_path / "b5.sqlite3")
    return cfg


def _install_main(monkeypatch, cfg) -> None:
    monkeypatch.setattr("feedkicker.config.load_config", lambda *a, **kw: cfg)
    monkeypatch.setattr(
        bitable_schema, "ensure_initialized", lambda bt, env: {"app_token": "a", "table_id": "t", "url": "u"}
    )
    monkeypatch.setattr(bitable_records, "sync_env", lambda *a, **k: 0)


def test_bitable_main_missing_lark_cli_returns_2(monkeypatch, tmp_path, caplog):
    cfg = _main_cfg(tmp_path)
    _install_main(monkeypatch, cfg)
    monkeypatch.setattr(bitable_lark, "lark_bin", lambda: None)
    with caplog.at_level(logging.ERROR):
        assert bitable.main(["--backfill", "--env", "test"]) == 2
    assert any("lark-cli" in r.getMessage() for r in caplog.records), "缺 lark-cli 必须 ERROR 且 rc2"


def test_bitable_main_lark_bin_not_found_returns_2(monkeypatch, tmp_path, caplog):
    cfg = _main_cfg(tmp_path)
    _install_main(monkeypatch, cfg)

    def no_lark():
        raise FileNotFoundError("找不到 lark-cli")

    monkeypatch.setattr(bitable_lark, "lark_bin", no_lark)
    with caplog.at_level(logging.ERROR):
        assert bitable.main(["--backfill", "--env", "test"]) == 2
    assert any("lark-cli" in r.getMessage() for r in caplog.records)


def test_bitable_backfill_dry_run_missing_verb_no_traceback(monkeypatch, tmp_path, caplog):
    cfg = _main_cfg(tmp_path)
    _install_main(monkeypatch, cfg)
    monkeypatch.setattr(bitable_lark, "_run", lambda *a, **k: FakeProc(0, "{}"))
    with caplog.at_level(logging.WARNING):
        assert bitable.main(["--backfill", "--dry-run", "--env", "test"]) == 0
    assert any("backfill" in r.getMessage() and "dry-run" in r.getMessage() for r in caplog.records)


# ── R5V-7 minimax base_resp.status_code 串 "0" 归一 ──


def test_minimax_parse_string_zero_is_missing_content_error():
    with pytest.raises(RuntimeError, match="缺少"):
        minimax._parse_outline_from_response({"base_resp": {"status_code": "0"}})


def test_minimax_parse_string_nonzero_still_error():
    with pytest.raises(RuntimeError, match="MiniMax 返回错误"):
        minimax._parse_outline_from_response({"base_resp": {"status_code": "1002"}})


def test_minimax_parse_choices_with_string_zero_ok():
    data = {
        "base_resp": {"status_code": "0"},
        "choices": [{"message": {"content": '{"title": "t", "slides": []}'}}],
    }
    assert minimax._parse_outline_from_response(data)["title"] == "t"
