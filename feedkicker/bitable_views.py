from __future__ import annotations

import json
import logging

from feedkicker import bitable_lark, bitable_schema

log = logging.getLogger(__name__)


def _view_id(app_token: str, table_id: str) -> str | None:
    proc = bitable_lark._run(["base", "+view-list", "--base-token", app_token, "--table-id", table_id])
    if not bitable_lark._ok(proc):
        return None
    views = bitable_lark._data(proc).get("views") or []
    for v in views:
        if v.get("view_name") == bitable_schema.VIEW_NAME or v.get("name") == bitable_schema.VIEW_NAME:
            return v.get("id")
    return views[0].get("id") if views else None


def _find_view(app_token: str, table_id: str, name: str) -> str | None:
    proc = bitable_lark._run(["base", "+view-list", "--base-token", app_token, "--table-id", table_id])
    if not bitable_lark._ok(proc):
        return None
    for v in bitable_lark._data(proc).get("views") or []:
        if v.get("view_name") == name or v.get("name") == name:
            vid = v.get("id") or v.get("view_id")
            if isinstance(vid, str) and vid:
                return vid
    return None


def setup_view(app_token: str, table_id: str) -> bool:
    vid = _view_id(app_token, table_id)
    if not vid:
        log.warning("未找到默认视图，跳过分组设置")
        return False
    g = bitable_lark._run(
        [
            "base", "+view-set-group",
            "--base-token", app_token,
            "--table-id", table_id,
            "--view-id", vid,
            "--json", json.dumps({"group_config": [{"field": "来源", "desc": False}]}, ensure_ascii=False),
        ]
    )
    s = bitable_lark._run(
        [
            "base", "+view-set-sort",
            "--base-token", app_token,
            "--table-id", table_id,
            "--view-id", vid,
            "--json", json.dumps({"sort_config": [{"field": "发布时间", "desc": True}]}, ensure_ascii=False),
        ]
    )
    return bitable_lark._ok(g) and bitable_lark._ok(s)


def set_tenant_readonly(app_token: str) -> bool:
    r1 = bitable_lark._run(
        ["drive", "permission.public", "patch", "--token", app_token,
         "--type", "bitable", "--data", '{"external_access": false}', "--yes"]
    )
    r2 = bitable_lark._run(
        ["drive", "permission.public", "patch", "--token", app_token,
         "--type", "bitable", "--data", '{"link_share_entity": "tenant_readable"}', "--yes"]
    )
    return bitable_lark._ok(r1) and bitable_lark._ok(r2)


def ensure_archive_date_field(app_token: str, table_id: str) -> bool:
    proc = bitable_lark._run(["base", "+field-list", "--base-token", app_token, "--table-id", table_id])
    names = set()
    if bitable_lark._ok(proc):
        d = bitable_lark._data(proc)
        items = d.get("fields") or d.get("items") or []
        if not isinstance(items, list) or not all(isinstance(f, dict) for f in items):
            raise RuntimeError(f"field-list 响应 fields 非 list[dict]: {str(items)[:200]}")
        for f in items:
            names.add(f.get("field_name") or f.get("name"))
    if "归档日期" in names:
        return True
    return bitable_lark._ok(bitable_lark._run(
        ["base", "+field-create", "--base-token", app_token, "--table-id", table_id,
         "--json", '{"name":"归档日期","type":"text"}'],
        timeout=60,
    ))


def create_date_view(app_token: str, table_id: str) -> bool:
    """创建「按日期」分组视图；同名已存在则复用并重设分组/排序（幂等，#209）。"""
    vid = _find_view(app_token, table_id, "按日期")
    if not vid:
        proc = bitable_lark._run(
            ["base", "+view-create", "--base-token", app_token,
             "--table-id", table_id,
             "--json", json.dumps({"name": "按日期", "type": "grid"}, ensure_ascii=False)],
            timeout=60,
        )
        if not bitable_lark._ok(proc):
            return False
        view = bitable_lark._data(proc).get("view") or {}
        raw = (view.get("view_id") or view.get("id")) if isinstance(view, dict) else None
        vid = raw if isinstance(raw, str) and raw else _view_id(app_token, table_id)
    if not vid:
        return False
    g = bitable_lark._run(
        ["base", "+view-set-group", "--base-token", app_token,
         "--table-id", table_id, "--view-id", vid,
         "--json", json.dumps({"group_config": [{"field": "归档日期", "desc": True}]},
                              ensure_ascii=False)],
    )
    s = bitable_lark._run(
        ["base", "+view-set-sort", "--base-token", app_token,
         "--table-id", table_id, "--view-id", vid,
         "--json", json.dumps({"sort_config": [{"field": "推送时间", "desc": True}]},
                              ensure_ascii=False)],
    )
    return bitable_lark._ok(g) and bitable_lark._ok(s)
