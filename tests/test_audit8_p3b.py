"""第八轮审计 P3-B（#349–#359）——失败优先验收。全离线：lark-cli/httpx 一律 mock。"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime

import pytest

from feedkicker import bitable, bitable_lark, extract_flow, salon_md, store, wiki, wiki_lark
from feedkicker.config import PROJECT_ROOT
from feedkicker.config_models import Config, ProviderConf

_TOPICS_JSON = json.dumps(
    {
        "topics": [
            {
                "话题名称": "话题A",
                "可使用工具": "工具X",
                "相关AI原理": "原理Y",
                "资讯链接": ["https://a/1"],
                "出处来源": ["量子位"],
            }
        ]
    },
    ensure_ascii=False,
)


class FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ── #349 宿主 env 泄漏隔离（conftest autouse） ──

_HOST_ENV = (
    "FEISHU_WEBHOOK",
    "FEISHU_SECRET",
    "TC_SALON_TOKEN",
    "TC_APP_ENV",
    "TC_DB",
    "MiniMax_Key",
    "MINIMAX_API_KEY",
    "DEEPSEEK_API_KEY",
    "TC_FEISHU_HOST",
)


def test_host_env_vars_isolated() -> None:
    assert [name for name in _HOST_ENV if name in os.environ] == []


# ── #350/#351/#352/#353/#354 文档同步 ──


def _doc(name: str) -> str:
    return (PROJECT_ROOT / "docs" / name).read_text(encoding="utf-8")


def test_prd_f25_enumerates_tc_extract() -> None:
    row = next(ln for ln in _doc("PRD.md").splitlines() if "命令行详解（9 命令）" in ln)
    assert "`tc-extract`" in row


def test_prd_drops_site_enabled_reference() -> None:
    assert "site.enabled" not in _doc("PRD.md")


def test_design_registers_strip_reasoning() -> None:
    text = _doc("DESIGN.md")
    assert "strip_reasoning" in text or "推理块" in text


def test_design_lists_split_modules() -> None:
    text = _doc("DESIGN.md")
    for name in ("reasoning.py", "minimax_transport.py", "minimax_parse.py"):
        assert name in text


def test_retention_lower_bound_documented() -> None:
    assert "静默钳为 1" in _doc("CLI.md")
    assert "静默钳为 1" in _doc("DESIGN.md")


# ── #355 无 id 页有界内容指纹 ──


def test_page_fingerprint_idless_data_rows_hashed() -> None:
    page = {"fields": ["链接"], "data": [{"链接": "https://e.com/x"} for _ in range(3)]}
    fp = bitable_lark._page_fingerprint(page)
    assert fp
    assert bitable_lark._page_guard("", page) == fp


def test_page_guard_idless_same_content_trips_second_page() -> None:
    page = {"fields": ["链接"], "data": [{"链接": "https://e.com/same"} for _ in range(200)]}
    fp = bitable_lark._page_fingerprint(page)
    with pytest.raises(RuntimeError, match="分页|offset"):
        bitable_lark._page_guard(fp, page)


def test_page_guard_idless_same_values_different_order_no_trip() -> None:
    first = {"fields": ["键"], "data": [{"键": "v0"}, {"键": "v1"}]}
    second = {"fields": ["键"], "data": [{"键": "v1"}, {"键": "v0"}]}
    fp = bitable_lark._page_fingerprint(first)
    assert fp and bitable_lark._page_guard(fp, second) == bitable_lark._page_fingerprint(second)


def test_page_fingerprint_id_order_still_insensitive() -> None:
    first = {"records": [{"record_id": "a"}, {"record_id": "b"}]}
    second = {"records": [{"record_id": "b"}, {"record_id": "a"}]}
    assert bitable_lark._page_fingerprint(first) == bitable_lark._page_fingerprint(second)


# ── #356 bitable.main 配置异常 rc2 ──


def test_bitable_main_missing_config_rc2(caplog) -> None:
    with caplog.at_level(logging.ERROR):
        rc = bitable.main(["--config", "/nonexistent/x.yaml"])
    assert rc == 2
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


# ── #357 wiki 标题首部 - ──


def test_sanitize_topic_strips_leading_hyphens() -> None:
    assert wiki.sanitize_topic("-X 模型") == "X 模型"
    assert wiki.sanitize_topic("--content=@./.env") == "content=@._.env"
    assert wiki.sanitize_topic("正常话题") == "正常话题"
    assert wiki.sanitize_topic("--") == "未命名"


def test_build_doc_title_no_leading_hyphen() -> None:
    title = wiki.build_doc_title("-X 模型", "2026-09-15")
    assert not title.startswith("-")
    assert title == "X 模型_2026-09-15_大纲"


def test_lark_doc_create_binds_title_with_equals(monkeypatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(bitable_lark, "_run", lambda args, **kw: seen.append(list(args)))
    wiki_lark.lark_doc_create("./x.md", "parent", "-X 模型")
    assert "--title=-X 模型" in seen[0]


# ── #358 tc-extract --apply 全失败 rc1 ──


def _extract_cfg() -> Config:
    cfg = Config(app_env="test")
    cfg.salon.app_token = "app"
    cfg.salon.table_id = "tbl"
    cfg.extract.providers["minimax"] = ProviderConf(api_key="sk")
    return cfg


def _patch_extract(monkeypatch, write_result: tuple[int, int, int]) -> None:
    monkeypatch.setattr(extract_flow.extract_source, "select_source", lambda conn, since_days, limit=None: [{"entry_key": "k"}])
    monkeypatch.setattr(extract_flow.extract_llm, "call_llm", lambda ex, prompt: _TOPICS_JSON)
    monkeypatch.setattr(extract_flow.extract_write, "write_topics", lambda *a, **k: write_result)


def test_extract_apply_all_writes_failed_rc1(monkeypatch, capsys) -> None:
    _patch_extract(monkeypatch, (0, 0, 1))
    conn = store.connect(":memory:")
    rc = extract_flow.run(_extract_cfg(), conn, apply=True)
    conn.close()
    assert rc == 1


def test_extract_apply_all_skipped_rc0(monkeypatch, capsys) -> None:
    _patch_extract(monkeypatch, (0, 1, 0))
    conn = store.connect(":memory:")
    rc = extract_flow.run(_extract_cfg(), conn, apply=True)
    conn.close()
    assert rc == 0


def test_extract_apply_partial_failure_rc0(monkeypatch, capsys) -> None:
    _patch_extract(monkeypatch, (1, 0, 1))
    conn = store.connect(":memory:")
    rc = extract_flow.run(_extract_cfg(), conn, apply=True)
    conn.close()
    assert rc == 0


# ── #359 salon_md 头日期用上海时区 ──


class _FixedDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        base = datetime(2026, 9, 14, 18, 0, tzinfo=UTC)
        return base.astimezone(tz) if tz else base


def test_build_combined_md_header_uses_shanghai_date(monkeypatch) -> None:
    monkeypatch.setattr(salon_md, "datetime", _FixedDatetime)
    outline = {"title": "t", "slides": [{"heading": "h", "bullets": ["a"]}]}
    md = salon_md.build_combined_md("T", outline, outline)
    assert "大纲归档 2026-09-15" in md
