"""第五轮重扫 P2（#242/#243/#244）——失败优先验收用例。

全部 lark-cli 经 monkeypatch 拦截：无真实子进程、无真实网络。
"""

from __future__ import annotations

import json
import random

import pytest

from feedkicker import bitable_backfill, bitable_lark, bitable_reseed
from feedkicker import topic as topic_mod


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ── #242 整页删净后重拉仅剩表头：不得再判列序异常 ──


def test_reseed_prod_full_page_then_header_only_ok(monkeypatch):
    header = "| _record_id | 标题 |\n| --- | --- |"
    full = header + "\n" + "\n".join(f"| recR{i:06d} | t{i} |" for i in range(200))
    list_calls = {"n": 0}

    def fake_run(args, stdin_text=None, timeout=120):
        if "+record-list" in args:
            list_calls["n"] += 1
            return FakeProc(0, stdout=full if list_calls["n"] == 1 else header)
        return FakeProc(0, stdout="{}")

    monkeypatch.setattr(bitable_lark, "_run", fake_run)
    assert bitable_reseed.purge_all_records("app", "tbl", env_name=None) == (200, True)
    assert list_calls["n"] == 2, "应删一屏后再拉一次确认为空表"


@pytest.mark.parametrize(
    "stdout",
    [
        "| _record_id | 标题 |\n| --- | --- |",
        "| _record_id | 标题 |\n| --- | --- |\n",
        "| _record_id | 标题 |\n|:--- | ---: |",
    ],
)
def test_reseed_prod_header_only_stdout_is_ok(monkeypatch, stdout):
    monkeypatch.setattr(bitable_lark, "_run", lambda *a, **k: FakeProc(0, stdout=stdout))
    assert bitable_reseed.purge_all_records("app", "tbl", env_name=None) == (0, True)


def test_reseed_prod_data_row_without_id_still_aborts(monkeypatch):
    page = "| _record_id | 标题 |\n| --- | --- |\n| 标题在前 | x |"
    monkeypatch.setattr(bitable_lark, "_run", lambda *a, **k: FakeProc(0, stdout=page))
    assert bitable_reseed.purge_all_records("app", "tbl", env_name=None) == (0, False)


# ── #243 行式指纹必须对行序抖动不敏感 ──


def test_page_fingerprint_order_insensitive_for_records_ids():
    first = {"records": [{"record_id": "recA"}, {"record_id": "recB"}]}
    second = {"records": [{"record_id": "recB"}, {"record_id": "recA"}]}
    assert bitable_lark._page_fingerprint(first) == bitable_lark._page_fingerprint(second)


def test_page_fingerprint_order_insensitive_for_top_record_ids():
    first = {
        "fields": ["键"],
        "record_ids": ["recA", "recB"],
        "data": [{"键": "1"}, {"键": "2"}],
    }
    second = {
        "fields": ["键"],
        "record_ids": ["recB", "recA"],
        "data": [{"键": "2"}, {"键": "1"}],
    }
    assert bitable_lark._page_fingerprint(first) == bitable_lark._page_fingerprint(second)


def test_page_fingerprint_idless_data_rows_empty_and_bounded_by_pages():
    """#N3：无 id 的 fields+data 不再按值内容哈希（指纹恒为 "" → 不误熔断），有界性交给 guard_pages。"""
    page = {"fields": ["键"], "data": [{"键": f"v{i}"} for i in range(5)]}

    assert bitable_lark._page_fingerprint(page) == ""
    assert bitable_lark._page_guard("", page) == ""


def _shuffled_rows_run(ids: list[str] | None, calls: dict[str, int]):
    rows = [{"键": f"v{i}"} for i in range(200)]

    def fake_run(args, stdin_text=None, timeout=120):
        if args[:2] == ["base", "--help"]:
            return FakeProc(0, "+record-batch-update")
        calls["n"] += 1
        order = list(range(len(rows)))
        random.Random(calls["n"]).shuffle(order)
        payload: dict = {"fields": ["键"], "data": [rows[i] for i in order]}
        if ids is not None:
            payload["record_ids"] = [ids[i] for i in order]
        return FakeProc(0, json.dumps({"data": payload}))

    return fake_run


def test_backfill_shuffled_row_order_ignoring_offset_raises_on_second_page(monkeypatch):
    calls = {"n": 0}
    ids = [f"recR{i:03d}" for i in range(200)]
    monkeypatch.setattr(bitable_lark, "_run", _shuffled_rows_run(ids, calls))
    with pytest.raises(RuntimeError, match="分页|offset"):
        bitable_backfill.backfill_empty_archive_dates("app", "tbl")
    assert calls["n"] == 2, "有 record id 时打乱行序 + 忽略 offset 必须第 2 页熔断"


def test_backfill_idless_shuffled_rows_bounded_by_page_cap(monkeypatch):
    """#303：无 id 时不再按值内容哈希误熔断，改由 guard_pages 页数上限兜底终止。"""
    calls = {"n": 0}
    monkeypatch.setattr(bitable_lark, "_MAX_PAGES", 3)
    monkeypatch.setattr(bitable_lark, "_run", _shuffled_rows_run(None, calls))
    with pytest.raises(RuntimeError, match="分页|offset|页数"):
        bitable_backfill.backfill_empty_archive_dates("app", "tbl")
    assert 2 < calls["n"] <= 5


# ── #244 topic 容器异常必须 raise，真正空页仍返回 [] ──


@pytest.mark.parametrize(
    "bad",
    [
        "not-a-dict",
        ["x"],
        {"records": "ab"},
        {"records": {"x": 1}},
        {"records": [{"record_id": "r1"}, "bad"]},
        {"data": 1},
        {},
        {"x": 1},
    ],
)
def test_topic_extract_container_abnormal_raises(bad):
    with pytest.raises(RuntimeError):
        topic_mod._extract_records(bad)


@pytest.mark.parametrize(
    "empty",
    [{"records": []}, {"items": []}, {"fields": ["标题"], "data": []}],
)
def test_topic_extract_empty_pages_return_empty(empty):
    assert topic_mod._extract_records(empty) == []


def test_fetch_selected_topics_bad_container_raises(monkeypatch):
    monkeypatch.setattr(
        topic_mod.bitable_lark,
        "_run",
        lambda *a, **k: FakeProc(0, json.dumps({"data": {"records": "oops"}})),
    )
    with pytest.raises(RuntimeError, match="records"):
        topic_mod.fetch_selected_topics("app", "tbl")
