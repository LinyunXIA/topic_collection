"""独立验证补遗（#361）——N1–N4 失败优先验收。全离线：lark-cli/httpx 一律 mock。"""

from __future__ import annotations

import json

import pytest

from feedkicker import bitable, bitable_backfill, bitable_lark, fetch
from feedkicker.config_models import Config
from feedkicker.extract_write import link_keys, plan_writes


class FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ── N1 is_url_token 收紧：点分非 URL token ──


@pytest.mark.parametrize("token", ["v2.0.1", "report.pdf", "1.2.3", "详见"])
def test_is_url_token_rejects_dotted_non_urls(token: str) -> None:
    assert fetch.is_url_token(token) is False


@pytest.mark.parametrize(
    "token",
    ["https://a.com/1", "example.com/path", "www.example.com/p", "example.com:8080/path"],
)
def test_is_url_token_accepts_urls(token: str) -> None:
    assert fetch.is_url_token(token) is True


def test_link_keys_ignores_version_and_filename_prose() -> None:
    assert link_keys("适用于 v2.0.1") == set()
    assert link_keys("报告 report.pdf") == set()


def test_plan_writes_distinct_urls_same_dotted_annotation() -> None:
    existing = link_keys("[A](https://a.com/1) 适用于 v2.0.1")
    topics = [
        {"话题名称": "B", "资讯链接": ["[B](https://b.com/2) 适用于 v2.0.1"]},
        {"话题名称": "C", "资讯链接": ["[C](https://c.com/3) 报告 report.pdf"]},
    ]

    picked, skipped = plan_writes(topics, "DS", "2026-09-15", set(), existing)

    assert [p["话题名称"] for p in picked] == ["B", "C"]
    assert skipped == []


# ── N2 backfill 中途 _ok:false 必须 raise → main rc2 ──


def _mid_page_failure_run(calls: dict):
    page = {
        "records": [
            {"record_id": f"r{i}", "fields": {"归档日期": "", "推送时间": "2026-01-01 10:00"}}
            for i in range(200)
        ]
    }

    def fake_run(args, stdin_text=None, timeout=120):
        if "+record-list" not in args:
            return FakeProc(0, "+record-batch-update")
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeProc(0, json.dumps({"data": page}))
        return FakeProc(0, json.dumps({"ok": False, "error": {"message": "boom"}}))

    return fake_run


def test_backfill_mid_page_business_failure_raises(monkeypatch) -> None:
    calls = {"n": 0}
    monkeypatch.setattr(bitable_lark, "_has_batch_verb", lambda: "+record-batch-update")
    monkeypatch.setattr(bitable_lark, "_run", _mid_page_failure_run(calls))

    with pytest.raises(RuntimeError, match="第 2 页|中止"):
        bitable_backfill.backfill_empty_archive_dates("app", "tbl", dry_run=True)


def test_bitable_main_backfill_business_failure_rc2(monkeypatch, tmp_path) -> None:
    cfg = Config(app_env="test")
    cfg.bitable.enabled = True
    cfg.bitable.app_token = "appReal"
    cfg.bitable.table_id = "tblReal"
    cfg.db_path = tmp_path / "b.sqlite3"
    monkeypatch.setattr("feedkicker.config.load_config", lambda *a, **k: cfg)
    from feedkicker import bitable_schema

    monkeypatch.setattr(
        bitable_schema, "ensure_initialized", lambda bt, env: {"app_token": "a", "table_id": "t", "url": "u"}
    )
    monkeypatch.setattr(bitable_lark, "lark_bin", lambda: "/usr/bin/lark-cli")
    monkeypatch.setattr(bitable_lark, "_has_batch_verb", lambda: "+record-batch-update")
    calls = {"n": 0}
    monkeypatch.setattr(bitable_lark, "_run", _mid_page_failure_run(calls))

    assert bitable.main(["--backfill", "--env", "test"]) == 2


# ── N3 页指纹认真实 record_id_list ──


def test_page_fingerprint_prefers_record_id_list_over_identical_data() -> None:
    first = {"fields": ["话题名称"], "data": [["A"]], "record_id_list": ["rec1", "rec2"]}
    second = {"fields": ["话题名称"], "data": [["A"]], "record_id_list": ["rec3", "rec4"]}

    fp = bitable_lark._page_fingerprint(first)

    assert fp
    assert fp != bitable_lark._page_fingerprint(second)
    assert bitable_lark._page_guard(fp, second) == bitable_lark._page_fingerprint(second)


def test_page_guard_record_id_list_identical_trips() -> None:
    page = {"fields": ["话题名称"], "data": [["A"]], "record_id_list": ["rec1", "rec2"]}
    fp = bitable_lark._page_fingerprint(page)

    with pytest.raises(RuntimeError, match="分页|offset"):
        bitable_lark._page_guard(fp, page)
