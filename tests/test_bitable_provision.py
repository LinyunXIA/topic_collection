"""bitable 自动建库集群（#166）：mock bitable._run，离线断言 lark-cli argv 与 ok:false 分流。

覆盖 find_base_by_title / create_base / create_table / get_table_id /
set_tenant_readonly / ensure_initialized / fields_for，不触网、不跑真实 lark-cli。
"""

from __future__ import annotations

import json

import pytest

from feedkicker import bitable


class FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _ok(payload: dict) -> FakeProc:
    return FakeProc(0, json.dumps({"ok": True, "data": payload}, ensure_ascii=False))


def _fail(message: str = "denied") -> FakeProc:
    return FakeProc(0, json.dumps({"ok": False, "error": {"message": message}}, ensure_ascii=False))


def _bt(app_token: str = "", table_id: str = "", url: str = ""):
    return type("BT", (), {"app_token": app_token, "table_id": table_id, "url": url})()


def test_fields_for_env_variants():
    prod = [f["name"] for f in bitable.fields_for("prod")]
    dev = [f["name"] for f in bitable.fields_for("dev")]
    test = [f["name"] for f in bitable.fields_for("test")]
    assert prod == ["标题", "链接", "来源", "摘要", "发布时间", "推送时间", "归档日期"]
    assert dev == ["标题", "环境", "链接", "来源", "摘要", "发布时间", "推送时间", "归档日期"]
    assert test == dev


def test_find_base_by_title_truncates_and_matches(monkeypatch: pytest.MonkeyPatch):
    title = "超长归档标题" * 7
    calls: list[list[str]] = []

    def fake_run(args, stdin_text=None, timeout=120):
        calls.append(list(args))
        return _ok(
            {"bases": [{"base_token": "appOther", "name": "别的表"}, {"base_token": "appHit", "name": title}]}
        )

    monkeypatch.setattr(bitable, "_run", fake_run)
    found = bitable.find_base_by_title(title)
    assert calls == [["base", "+title-resolve", "--title", title[:30]]]
    assert found == {"app_token": "appHit", "url": bitable.base_url("appHit")}


def test_find_base_by_title_ok_false_returns_none(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(bitable, "_run", lambda *a, **kw: _fail("not found"))
    assert bitable.find_base_by_title("资讯归档") is None


def test_find_base_by_title_token_fallback(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(bitable, "_run", lambda *a, **kw: _ok({"token": "appTok"}))
    found = bitable.find_base_by_title("资讯归档")
    assert found == {"app_token": "appTok", "url": bitable.base_url("appTok")}


def test_create_base_sends_fields_and_returns_url(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[list[str], float]] = []

    def fake_run(args, stdin_text=None, timeout=120):
        calls.append((list(args), timeout))
        return _ok({"base": {"base_token": "appNew", "url": ""}})

    monkeypatch.setattr(bitable, "_run", fake_run)
    created = bitable.create_base("dev 归档", app_env="dev")

    args, timeout = calls[0]
    fields_raw = args[args.index("--fields") + 1]
    assert args[:6] == ["base", "+base-create", "--name", "dev 归档", "--table-name", bitable.TABLE_NAME]
    assert args[args.index("--time-zone") + 1] == "Asia/Shanghai"
    assert json.loads(fields_raw) == bitable.fields_for("dev")
    assert timeout == 120
    assert created == {"app_token": "appNew", "url": bitable.base_url("appNew")}


def test_create_base_failures(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(bitable, "_run", lambda *a, **kw: _fail("bad request"))
    with pytest.raises(RuntimeError, match="创建 Base 失败"):
        bitable.create_base("X")
    monkeypatch.setattr(bitable, "_run", lambda *a, **kw: _ok({"base": {}}))
    with pytest.raises(RuntimeError, match="缺少 token"):
        bitable.create_base("X")


def test_get_table_id_matches_by_name(monkeypatch: pytest.MonkeyPatch):
    calls: list[list[str]] = []

    def fake_run(args, stdin_text=None, timeout=120):
        calls.append(list(args))
        return _ok(
            {"tables": [{"name": "其他", "id": "tblOther"}, {"name": bitable.TABLE_NAME, "id": "tblHit"}]}
        )

    monkeypatch.setattr(bitable, "_run", fake_run)
    assert bitable.get_table_id("app1") == "tblHit"
    assert calls == [["base", "+table-list", "--base-token", "app1"]]


def test_get_table_id_missing_or_failed_returns_none(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(bitable, "_run", lambda *a, **kw: _ok({"tables": [{"name": "别的", "id": "x"}]}))
    assert bitable.get_table_id("app1") is None
    monkeypatch.setattr(bitable, "_run", lambda *a, **kw: _fail())
    assert bitable.get_table_id("app1") is None


def test_create_table_returns_id_and_fields(monkeypatch: pytest.MonkeyPatch):
    calls: list[list[str]] = []

    def fake_run(args, stdin_text=None, timeout=120):
        calls.append(list(args))
        return _ok({"table_id": "tblNew"})

    monkeypatch.setattr(bitable, "_run", fake_run)
    assert bitable.create_table("app1", app_env="test") == "tblNew"
    args = calls[0]
    assert args[:5] == ["base", "+table-create", "--base-token", "app1", "--name"]
    assert args[args.index("--name") + 1] == bitable.TABLE_NAME
    assert json.loads(args[args.index("--fields") + 1]) == bitable.fields_for("test")


def test_create_table_failures(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(bitable, "_run", lambda *a, **kw: _fail())
    with pytest.raises(RuntimeError, match="创建数据表失败"):
        bitable.create_table("app1")
    monkeypatch.setattr(bitable, "_run", lambda *a, **kw: _ok({}))
    with pytest.raises(RuntimeError, match="缺少 table_id"):
        bitable.create_table("app1")


def test_set_tenant_readonly_two_patches(monkeypatch: pytest.MonkeyPatch):
    calls: list[list[str]] = []

    def fake_run(args, stdin_text=None, timeout=120):
        calls.append(list(args))
        return FakeProc(0, "{}")

    monkeypatch.setattr(bitable, "_run", fake_run)
    assert bitable.set_tenant_readonly("appX") is True
    assert len(calls) == 2
    assert calls[0] == [
        "drive", "permission.public", "patch", "--token", "appX",
        "--type", "bitable", "--data", '{"external_access": false}', "--yes",
    ]
    assert calls[1][:6] == calls[0][:6]
    assert calls[1][calls[1].index("--data") + 1] == '{"link_share_entity": "tenant_readable"}'


def test_set_tenant_readonly_business_failure(monkeypatch: pytest.MonkeyPatch):
    calls: list[list[str]] = []

    def fake_run(args, stdin_text=None, timeout=120):
        calls.append(list(args))
        return _fail() if len(calls) == 2 else FakeProc(0, "{}")

    monkeypatch.setattr(bitable, "_run", fake_run)
    assert bitable.set_tenant_readonly("appX") is False


def test_ensure_initialized_uses_configured_tokens(monkeypatch: pytest.MonkeyPatch):
    def forbid(*a, **kw):
        raise AssertionError("已配置 token 不得调 lark-cli")

    monkeypatch.setattr(bitable, "_run", forbid)
    info = bitable.ensure_initialized(_bt("appCfg", "tblCfg", "https://x/base"))
    assert info == {"app_token": "appCfg", "table_id": "tblCfg", "url": "https://x/base"}


def test_ensure_initialized_creates_base_then_table(monkeypatch: pytest.MonkeyPatch):
    steps: list[tuple] = []
    monkeypatch.setattr(
        bitable, "find_base_by_title", lambda title: steps.append(("find", title)) or None
    )
    monkeypatch.setattr(
        bitable,
        "create_base",
        lambda title, env: steps.append(("create_base", title, env))
        or {"app_token": "appNew", "url": "https://new"},
    )
    monkeypatch.setattr(
        bitable, "get_table_id", lambda tok: steps.append(("get_table", tok)) or None
    )
    monkeypatch.setattr(
        bitable,
        "create_table",
        lambda tok, env: steps.append(("create_table", tok, env)) or "tblNew",
    )
    info = bitable.ensure_initialized(_bt(), app_env="dev")
    assert info["app_token"] == "appNew"
    assert info["table_id"] == "tblNew"
    assert steps == [
        ("find", bitable.BASE_TITLES["dev"]),
        ("create_base", bitable.BASE_TITLES["dev"], "dev"),
        ("get_table", "appNew"),
        ("create_table", "appNew", "dev"),
    ]


def test_ensure_initialized_reuses_found_base(monkeypatch: pytest.MonkeyPatch):
    def no_create(*a, **kw):
        raise AssertionError("已找到 Base 不得重复创建")

    monkeypatch.setattr(
        bitable, "find_base_by_title", lambda title: {"app_token": "appFound", "url": "https://found"}
    )
    monkeypatch.setattr(bitable, "create_base", no_create)
    monkeypatch.setattr(bitable, "get_table_id", lambda tok: "tblFound")
    info = bitable.ensure_initialized(_bt(), app_env="prod")
    assert info["app_token"] == "appFound"
    assert info["table_id"] == "tblFound"
