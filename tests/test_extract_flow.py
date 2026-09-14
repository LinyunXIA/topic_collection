"""F32：tc-extract 参数解析 / 退出码 / dry-run 零写 / apply / max_calls / 失败重试（全 mock 离线）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from feedkicker import bitable_lark, extract_flow, extract_llm, store
from feedkicker.config import load_config

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


def _json_from_args(args: list[str]) -> dict[str, Any]:
    raw = args[args.index("--json") + 1]
    if raw.startswith("@"):
        return json.loads(Path(raw[1:]).read_text(encoding="utf-8"))
    return json.loads(raw)


@pytest.fixture(autouse=True)
def _no_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("MiniMax_Key", "MINIMAX_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def _write_cfg(
    tmp_path: Path,
    *,
    provider: str = "minimax",
    prompt_file: str = "prompts/extract.md",
    enabled: bool = True,
    key: str = "sk-test",
    app_token: str = "appTest",
    table_id: str = "tblTest",
) -> Path:
    path = tmp_path / "cfg.yaml"
    path.write_text(
        "salon:\n"
        f"  app_token: \"{app_token}\"\n"
        f"  table_id: \"{table_id}\"\n"
        "extract:\n"
        f"  enabled: {str(enabled).lower()}\n"
        f"  provider: {provider}\n"
        f"  prompt_file: {prompt_file}\n"
        "  since_days: 7\n"
        "  batch_size: 2\n"
        "  max_calls: 0\n"
        "  providers:\n"
        f"    {provider}:\n"
        f"      api_key: \"{key}\"\n",
        encoding="utf-8",
    )
    return path


def _patch_lark(monkeypatch: pytest.MonkeyPatch, calls: list[list[str]]) -> None:
    def fake_run(args, stdin_text=None, timeout=120):
        calls.append(list(args))
        if "+record-list" in args:
            return FakeProc(0, '{"data": {"records": []}}')
        return FakeProc(0, "{}")

    monkeypatch.setattr(bitable_lark, "_run", fake_run)


def _items(n: int) -> list[dict[str, Any]]:
    return [
        {
            "feed_id": "量子位",
            "entry_key": f"k{i}",
            "title": f"标题{i}",
            "url": f"https://e/{i}",
            "description": "摘要",
            "published_at": None,
            "first_seen": "2026-09-14T00:00:00Z",
        }
        for i in range(n)
    ]


def _last_summary(out: str) -> dict[str, Any]:
    lines = [ln for ln in out.splitlines() if ln.startswith("{")]
    return json.loads(lines[-1])


def test_main_help_rc0(capsys) -> None:
    with pytest.raises(SystemExit) as ei:
        extract_flow.main(["--help"])

    assert ei.value.code == 0
    assert "--apply" in capsys.readouterr().out


def test_main_both_modes_rejected() -> None:
    with pytest.raises(SystemExit) as ei:
        extract_flow.main(["--apply", "--dry-run"])

    assert ei.value.code == 2


def test_main_missing_config_rc2(tmp_path) -> None:
    assert extract_flow.main(["--config", str(tmp_path / "nope.yaml")]) == 2


def test_main_dry_run_rc0_zero_write(tmp_path, monkeypatch, capsys) -> None:
    cfg = _write_cfg(tmp_path)
    calls: list[list[str]] = []
    _patch_lark(monkeypatch, calls)
    monkeypatch.setattr(extract_flow.extract_source, "select_source", lambda conn, since_days, limit=None: _items(2))
    monkeypatch.setattr(extract_flow.extract_llm, "call_llm", lambda ex, prompt: _TOPICS_JSON)

    rc = extract_flow.main(["--dry-run", "--config", str(cfg), "--db", str(tmp_path / "t.sqlite3")])

    out = capsys.readouterr().out
    assert rc == 0
    assert "待写选题 1 个" in out and "话题A" in out
    assert [c for c in calls if "+record-batch-create" in c] == []
    assert _last_summary(out)["mode"] == "dry-run"


def test_main_apply_writes_mapped_record(tmp_path, monkeypatch, capsys) -> None:
    cfg = _write_cfg(tmp_path)
    calls: list[list[str]] = []
    created: list[dict[str, Any]] = []

    def fake_run(args, stdin_text=None, timeout=120):
        calls.append(list(args))
        if "+record-list" in args:
            return FakeProc(0, '{"data": {"records": []}}')
        if "+record-batch-create" in args:
            created.extend(_json_from_args(args)["create_records"])
        return FakeProc(0, "{}")

    monkeypatch.setattr(bitable_lark, "_run", fake_run)
    monkeypatch.setattr(extract_flow.extract_source, "select_source", lambda conn, since_days, limit=None: _items(2))
    monkeypatch.setattr(extract_flow.extract_llm, "call_llm", lambda ex, prompt: _TOPICS_JSON)

    rc = extract_flow.main(["--apply", "--config", str(cfg), "--db", str(tmp_path / "t.sqlite3")])

    out = capsys.readouterr().out
    assert rc == 0
    assert created[0]["话题名称"] == "话题A"
    assert created[0]["讨论状态"] == ["未讨论"]
    assert created[0]["提取工具"] == ["MMax"]
    assert created[0]["资讯链接"] == "https://a/1"
    stats = _last_summary(out)
    assert stats["mode"] == "apply" and stats["written"] == 1 and stats["pending"] == 0


def test_batch_failure_retried_then_skipped(tmp_path, monkeypatch, capsys) -> None:
    cfg = _write_cfg(tmp_path)
    calls: list[list[str]] = []
    _patch_lark(monkeypatch, calls)
    monkeypatch.setattr(extract_flow.extract_source, "select_source", lambda conn, since_days, limit=None: _items(4))
    state = {"n": 0}

    def fake_llm(ex, prompt):
        state["n"] += 1
        if state["n"] <= 2:
            raise RuntimeError("boom")
        return _TOPICS_JSON

    monkeypatch.setattr(extract_flow.extract_llm, "call_llm", fake_llm)

    rc = extract_flow.main(["--dry-run", "--config", str(cfg), "--db", str(tmp_path / "t.sqlite3")])

    out = capsys.readouterr().out
    stats = _last_summary(out)
    assert rc == 0
    assert state["n"] == 3
    assert stats["failed_batches"] == 1 and stats["llm_calls"] == 3 and stats["topics"] == 1
    assert stats["empty_batches"] == 0


def test_empty_topics_batch_counted_separately(tmp_path, monkeypatch, capsys) -> None:
    cfg = _write_cfg(tmp_path)
    calls: list[list[str]] = []
    _patch_lark(monkeypatch, calls)
    monkeypatch.setattr(extract_flow.extract_source, "select_source", lambda conn, since_days, limit=None: _items(4))
    monkeypatch.setattr(extract_flow.extract_llm, "call_llm", lambda ex, prompt: '{"topics": []}')

    rc = extract_flow.main(["--dry-run", "--config", str(cfg), "--db", str(tmp_path / "t.sqlite3")])

    out = capsys.readouterr().out
    stats = _last_summary(out)
    assert rc == 0
    assert stats["empty_batches"] == 2 and stats["failed_batches"] == 0
    assert stats["llm_calls"] == 2 and stats["topics"] == 0


def test_bad_json_batch_counts_failed_not_empty(tmp_path, monkeypatch, capsys) -> None:
    cfg = _write_cfg(tmp_path)
    calls: list[list[str]] = []
    _patch_lark(monkeypatch, calls)
    monkeypatch.setattr(extract_flow.extract_source, "select_source", lambda conn, since_days, limit=None: _items(4))
    monkeypatch.setattr(extract_flow.extract_llm, "call_llm", lambda ex, prompt: "抱歉，无法提炼")

    rc = extract_flow.main(["--dry-run", "--config", str(cfg), "--db", str(tmp_path / "t.sqlite3")])

    out = capsys.readouterr().out
    stats = _last_summary(out)
    assert rc == 0
    assert stats["failed_batches"] == 2 and stats["empty_batches"] == 0
    assert stats["llm_calls"] == 2


def test_bad_topic_item_dropped_keeps_batch(tmp_path, monkeypatch, capsys) -> None:
    cfg = _write_cfg(tmp_path)
    calls: list[list[str]] = []
    _patch_lark(monkeypatch, calls)
    monkeypatch.setattr(extract_flow.extract_source, "select_source", lambda conn, since_days, limit=None: _items(2))
    mixed = json.dumps(
        {
            "topics": [
                {"话题名称": "坏话题"},
                {
                    "话题名称": "好话题",
                    "可使用工具": "工具X",
                    "相关AI原理": "原理Y",
                    "资讯链接": ["https://a/1"],
                    "出处来源": ["量子位"],
                },
            ]
        },
        ensure_ascii=False,
    )
    monkeypatch.setattr(extract_flow.extract_llm, "call_llm", lambda ex, prompt: mixed)

    rc = extract_flow.main(["--dry-run", "--config", str(cfg), "--db", str(tmp_path / "t.sqlite3")])

    out = capsys.readouterr().out
    stats = _last_summary(out)
    assert rc == 0 and stats["topics"] == 1
    assert stats["failed_batches"] == 0 and stats["empty_batches"] == 0


def test_refine_batches_http_budget_two_per_batch(tmp_path, monkeypatch) -> None:
    cfg_path = _write_cfg(tmp_path)
    cfg = load_config(cfg_path, app_env="test")
    posts: list[str] = []

    class Resp429:
        status_code = 429
        text = '{"error": "rate"}'

        def json(self):
            return {"error": "rate"}

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        posts.append(url)
        return Resp429()

    monkeypatch.setattr(extract_flow.extract_llm.httpx, "post", fake_post)

    collected, calls, failed, empty = extract_llm.refine_batches(
        cfg.extract, "模板", [[{"title": "标题", "url": "https://a/1"}]], 0
    )

    assert collected == [] and failed == 1 and empty == 0
    assert calls == 2 and len(posts) == 2


def test_max_calls_stops_remaining_batches(tmp_path, monkeypatch, capsys) -> None:
    cfg = _write_cfg(tmp_path)
    calls: list[list[str]] = []
    _patch_lark(monkeypatch, calls)
    monkeypatch.setattr(extract_flow.extract_source, "select_source", lambda conn, since_days, limit=None: _items(4))
    state = {"n": 0}

    def fake_llm(ex, prompt):
        state["n"] += 1
        return _TOPICS_JSON

    monkeypatch.setattr(extract_flow.extract_llm, "call_llm", fake_llm)

    rc = extract_flow.main(
        ["--dry-run", "--max-calls", "1", "--config", str(cfg), "--db", str(tmp_path / "t.sqlite3")]
    )

    out = capsys.readouterr().out
    stats = _last_summary(out)
    assert rc == 0 and state["n"] == 1
    assert stats["llm_calls"] == 1 and stats["topics"] == 1 and stats["batches"] == 2


@pytest.mark.parametrize(
    "kwargs",
    [
        {"prompt_file": "prompts/nope.md"},
        {"provider": "openai"},
        {"key": ""},
        {"app_token": ""},
    ],
)
def test_config_errors_rc2(tmp_path, kwargs) -> None:
    cfg = _write_cfg(tmp_path, **kwargs)

    assert extract_flow.main(["--dry-run", "--config", str(cfg), "--db", str(tmp_path / "t.sqlite3")]) == 2


def test_no_source_rows_rc0(tmp_path, monkeypatch, capsys) -> None:
    cfg = _write_cfg(tmp_path)
    calls: list[list[str]] = []
    _patch_lark(monkeypatch, calls)
    monkeypatch.setattr(extract_flow.extract_source, "select_source", lambda conn, since_days, limit=None: [])

    rc = extract_flow.main(["--config", str(cfg), "--db", str(tmp_path / "t.sqlite3")])

    out = capsys.readouterr().out
    stats = _last_summary(out)
    assert rc == 0 and stats["batches"] == 0 and stats["topics"] == 0


def test_run_uses_cfg_defaults_and_cli_overrides(tmp_path, monkeypatch) -> None:
    cfg_path = _write_cfg(tmp_path)
    cfg = load_config(cfg_path, app_env="test")
    cfg.extract.since_days = 3
    seen: dict[str, Any] = {}
    calls: list[list[str]] = []
    _patch_lark(monkeypatch, calls)
    monkeypatch.setattr(extract_flow.extract_llm, "call_llm", lambda ex, prompt: '{"topics": []}')

    def fake_select(conn, since_days, limit=None):
        seen.update({"since_days": since_days, "limit": limit})
        return []

    monkeypatch.setattr(extract_flow.extract_source, "select_source", fake_select)
    conn = store.connect(tmp_path / "t.sqlite3")
    try:
        assert extract_flow.run(cfg, conn, apply=False) == 0
        assert seen["since_days"] == 3
        assert extract_flow.run(cfg, conn, apply=False, since_days=1, limit=5) == 0
        assert seen == {"since_days": 1, "limit": 5}
    finally:
        conn.close()
