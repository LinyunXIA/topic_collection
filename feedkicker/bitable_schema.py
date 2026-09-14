from __future__ import annotations

import json
import logging
from typing import Any

from feedkicker import bitable_lark
from feedkicker.feishu_host import feishu_host

log = logging.getLogger(__name__)

BASE_TITLE = "AI 资讯归档"
BASE_TITLE_DEV_TEST = "AI 资讯归档 · dev-test"
BASE_TITLES = {"prod": BASE_TITLE, "dev": BASE_TITLE_DEV_TEST, "test": BASE_TITLE_DEV_TEST}
BASE_TITLE_DEFAULT = BASE_TITLE
TABLE_NAME = "文章"
VIEW_NAME = "表格"

_FIELDS = [
    {"name": "标题", "type": "text"},
    {"name": "链接", "type": "url"},
    {"name": "来源", "type": "text"},
    {"name": "摘要", "type": "text"},
    {"name": "发布时间", "type": "datetime"},
    {"name": "推送时间", "type": "datetime"},
    {"name": "归档日期", "type": "text"},
]


def fields_for(app_env: str) -> list[dict[str, Any]]:
    if app_env in ("dev", "test"):
        return [_FIELDS[0]] + [{"name": "环境", "type": "text"}] + _FIELDS[1:]
    return list(_FIELDS)


def base_url(app_token: str) -> str:
    return f"https://{feishu_host()}/base/{app_token}"


def find_base_by_title(title: str) -> dict[str, Any] | None:
    """按标题**精确**匹配既有 Base（prod 标题是 dev-test 标题前缀，子串匹配会串 Base，#264）。"""
    proc = bitable_lark._run(["base", "+title-resolve", "--title", title[:30]])
    if not bitable_lark._ok(proc):
        return None
    d = bitable_lark._data(proc)
    for cand in (
        d.get("bases"),
        d.get("items"),
        d.get("files") or d.get("docs"),
    ):
        if isinstance(cand, list):
            for b in cand:
                tok = b.get("base_token") or b.get("token") or ""
                if tok and (b.get("name") == title or str(b.get("title", "")) == title):
                    return {"app_token": tok, "url": b.get("url") or base_url(tok)}
    if isinstance(d.get("token"), str) and d["token"]:
        return {"app_token": d["token"], "url": d.get("url") or base_url(d["token"])}
    return None


def create_base(title: str, app_env: str = "prod") -> dict[str, Any]:
    proc = bitable_lark._run(
        [
            "base",
            "+base-create",
            "--name",
            title,
            "--table-name",
            TABLE_NAME,
            "--fields",
            json.dumps(fields_for(app_env), ensure_ascii=False),
            "--time-zone",
            "Asia/Shanghai",
        ],
        timeout=120,
    )
    if not bitable_lark._ok(proc):
        raise RuntimeError(f"创建 Base 失败: {proc.stderr[:200] if proc else 'unknown'}")
    d = bitable_lark._data(proc)
    base = d.get("base") or {}
    app_token = base.get("base_token") or ""
    if not app_token:
        raise RuntimeError(f"Base 创建响应缺少 token: {str(d)[:200]}")
    return {"app_token": app_token, "url": base.get("url") or base_url(app_token)}


def get_table_id(app_token: str) -> str | None:
    proc = bitable_lark._run(["base", "+table-list", "--base-token", app_token])
    if not bitable_lark._ok(proc):
        return None
    for t in bitable_lark._data(proc).get("tables") or []:
        if t.get("name") == TABLE_NAME:
            return t.get("id")
    return None


def create_table(app_token: str, app_env: str = "prod") -> str:
    proc = bitable_lark._run(
        [
            "base",
            "+table-create",
            "--base-token",
            app_token,
            "--name",
            TABLE_NAME,
            "--fields",
            json.dumps(fields_for(app_env), ensure_ascii=False),
        ],
        timeout=120,
    )
    if not bitable_lark._ok(proc):
        raise RuntimeError("创建数据表失败")
    table_id = bitable_lark._data(proc).get("table_id")
    if not table_id:
        stdout = proc.stdout if proc is not None else ""
        raise RuntimeError(f"创建数据表响应缺少 table_id: {stdout[:200]}")
    return table_id


def _configured(value: Any) -> bool:
    """真值且非 `<...>` 占位才视为已配置（`.example` 默认态占位不得当真 token 使用，#262）。"""
    return bool(value) and "<" not in str(value)


def ensure_initialized(bt: Any, app_env: str = "prod") -> dict[str, Any]:
    """解析/创建 Base 与数据表，并把解析结果回写到空配置字段（A1）。

    push 在 sync_env 后按 bt.url/bt.app_token 重算详情按钮链接；
    若不回写，自动解析出的 token 会丢在局部变量里，卡片退化成 …/base/。
    占位 token（`<...>`）按「未配置」处理：若用真值判断，`.example` 默认占位会被当成既有
     Base 直接跳过解析，后续 lark 调用全业务失败却被静默吞掉（#262）。
    """
    title = BASE_TITLES.get(app_env, BASE_TITLE_DEFAULT)
    app_token = bt.app_token if _configured(bt.app_token) else ""
    table_id = bt.table_id if _configured(bt.table_id) else ""
    url = bt.url or (base_url(app_token) if app_token else "")
    if not app_token:
        found = find_base_by_title(title) or create_base(title, app_env)
        app_token = found["app_token"]
        url = found.get("url") or base_url(app_token)
    if not table_id:
        table_id = get_table_id(app_token) or create_table(app_token, app_env)
    if not _configured(bt.app_token):
        bt.app_token = app_token
    if not _configured(bt.table_id):
        bt.table_id = table_id
    if not bt.url:
        bt.url = url
    return {"app_token": app_token, "table_id": table_id, "url": url}
