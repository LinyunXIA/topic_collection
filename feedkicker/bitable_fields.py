"""field-list 分页读取（score_flow / bitable_views / topic 共用，防 >100 列宽表只读首页，#R10-28）。"""

from __future__ import annotations

from typing import Any

from feedkicker import bitable_lark


def read_fields(
    app_token: str, table_id: str, *, timeout: float = 60, tolerant: bool = False
) -> list[dict[str, Any]]:
    """分页读全表字段 dict（offset + 页数 + 页指纹三重兜底）；`fields` 非 list[dict] 一律 raise。

    `tolerant=True` 时首屏/中途页 `_ok` 失败返回已读部分（供 --init 尽力而为），否则 raise。
    """
    fields: list[dict[str, Any]] = []
    offset = 0
    prev_fp = ""
    while True:
        bitable_lark._guard_offset(offset)
        bitable_lark.guard_pages(offset // bitable_lark._CHUNK + 1)
        proc = bitable_lark._run(
            [
                "base", "+field-list",
                "--base-token", app_token,
                "--table-id", table_id,
                "--limit", str(bitable_lark._CHUNK),
                "--offset", str(offset),
            ],
            timeout=timeout,
        )
        if not bitable_lark._ok(proc):
            if tolerant:
                return fields
            raise RuntimeError("读取目标表字段列表失败")
        data = bitable_lark._data(proc)
        prev_fp = bitable_lark._page_guard(prev_fp, data)
        items = data.get("fields") or data.get("items") or []
        if not isinstance(items, list) or not all(isinstance(f, dict) for f in items):
            raise RuntimeError(f"field-list 响应 fields 非 list[dict]: {str(items)[:200]}")
        fields.extend(items)
        if len(items) < bitable_lark._CHUNK:
            return fields
        offset += bitable_lark._CHUNK
