"""第五轮审计 P2（#235/#236）——归档健壮性与脱敏 canary。

#235 全部 lark-cli 经 monkeypatch 拦截；#236 见 tests/test_hygiene.py（canary 哈希化）。
"""

from __future__ import annotations

import json
import logging

import pytest

from feedkicker import bitable_lark, bitable_purge, bitable_records, bitable_reseed


class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ── #235(a) env-less / 空「环境」行保守不删但必须 WARNING ──


def test_env_record_ids_warns_on_envless_rows(monkeypatch, caplog):
    monkeypatch.setattr(
        bitable_purge,
        "_list_records",
        lambda *a, **k: (
            [
                ("recDevKeep", {"环境": "dev"}),
                ("recNoEnv", {}),
                ("recEmptyEnv", {"环境": ""}),
                ("recTestKeep", {"环境": "test"}),
            ],
            True,
            True,
        ),
    )
    with caplog.at_level(logging.WARNING):
        ids, ok = bitable_reseed._env_record_ids("app", "tbl", "dev")
    assert ok is True
    assert ids == ["recDevKeep"], "保守策略：只删匹配行，env-less 保留"
    assert any("环境" in r.getMessage() for r in caplog.records), "env-less 被跳过必须 WARNING"


# ── #235(b) prod markdown：非空但零解析必须中止，空 stdout 视为真空表 ──


def test_reseed_prod_markdown_nonempty_zero_ids_aborts(monkeypatch, caplog):
    page = "| _record_id | 标题 |\n| --- | --- |\n| 无 id 行 | x |"
    monkeypatch.setattr(bitable_lark, "_run", lambda *a, **k: FakeProc(0, stdout=page))
    with caplog.at_level(logging.WARNING):
        deleted, ok = bitable_reseed.purge_all_records("app", "tbl", env_name=None)
    assert (deleted, ok) == (0, False), "markdown 非空却解析不出 id：不得零删除报成功"
    assert any("record" in r.getMessage() for r in caplog.records)


def test_reseed_prod_markdown_empty_stdout_is_ok(monkeypatch):
    monkeypatch.setattr(bitable_lark, "_run", lambda *a, **k: FakeProc(0, stdout="  \n"))
    assert bitable_reseed.purge_all_records("app", "tbl", env_name=None) == (0, True)


# ── #235(c) fields+data 行式缺「链接」字段：rows 非空必须 raise ──


def test_existing_links_rows_without_link_field_raises(monkeypatch):
    payload = {"fields": ["标题"], "data": [{"标题": "x"}]}
    monkeypatch.setattr(
        bitable_lark, "_run", lambda *a, **k: FakeProc(0, stdout=json.dumps({"data": payload}))
    )
    with pytest.raises(RuntimeError, match="链接"):
        bitable_records.existing_links("app", "tbl")


def test_existing_links_empty_rows_without_link_field_ok(monkeypatch):
    payload = {"fields": ["标题"], "data": []}
    monkeypatch.setattr(
        bitable_lark, "_run", lambda *a, **k: FakeProc(0, stdout=json.dumps({"data": payload}))
    )
    assert bitable_records.existing_links("app", "tbl") == set()
