"""topic 响应记录归一（自 topic.py 抽出以守住 ≤200 行门，DESIGN §21.2）。"""

from __future__ import annotations

from typing import Any

_ID_LIST_KEYS = (
    "record_ids", "recordIds", "ids", "record_id_list", "recordId_list", "recordIdList",
)


def row_ids(page: Any) -> list[str]:
    """从 record-list 响应提取行 id（统一 purge/reseed/backfill 的矩阵形态口径，#373）。

    优先 `records[]/items[]` 的 `record_id|id|recordId`；无记录容器时回退顶层
    `record_ids/recordIds/ids/record_id_list/recordId_list/recordIdList`（真实 lark-cli
    矩阵形态的 id 在顶层 `record_id_list`，#361）。非 dict / 无 id 源返回 []。
    """
    if not isinstance(page, dict):
        return []
    records = page.get("records") or page.get("items")
    if isinstance(records, list) and records and all(isinstance(r, dict) for r in records):
        return [str(r.get("record_id") or r.get("id") or r.get("recordId") or "") for r in records]
    for key in _ID_LIST_KEYS:
        raw = page.get(key)
        if isinstance(raw, list):
            return [str(i) for i in raw if i]
    return []


def _extract_records(data: Any) -> list[dict[str, Any]]:
    """记录提取：容器异常（顶层非 dict / 无可识别容器键 / records 非空但非 list[dict] / data 非 list / fields 非 list）抛 RuntimeError。

    对齐 existing_links 的「必须中止」：上游 schema 漂移不得被静默当作「无已选题」（#244）；
    顶层 dict 但既无 records/items 也无 fields+data（及 record_ids 等行式容器键）即不可识别响应
    （如 rc0 的 `{}` / `{"ok":true,"data":{}}`）必须 raise，不得当合法空页（#326）；fields 为
    dict/str 等非 list 语义时同样 raise，不得静默降级空列（#301）。**显式**空形态
    （records/items 空 list、fields+data 空）才返回 []；响应兼容 records/items 包装与
    data.fields+data.data 行式两种形态。
    """
    if not isinstance(data, dict):
        raise RuntimeError(f"topic 响应顶层非对象: {type(data).__name__}")
    has_container = (
        "records" in data
        or "items" in data
        or ("fields" in data and "data" in data)
        or any(k in data for k in _ID_LIST_KEYS)
    )
    if not has_container:
        raise RuntimeError(f"topic 响应无可识别容器键（无 records/items/fields+data）: {str(data)[:200]}")
    records = data.get("records") or data.get("items")
    if records:
        if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
            raise RuntimeError(f"topic records 非 list[dict]: {type(records).__name__}")
        return list(records)
    fields_raw = data.get("fields")
    if fields_raw is not None and not isinstance(fields_raw, list):
        raise RuntimeError(f"topic fields 容器非 list: {type(fields_raw).__name__}")
    fields: list[Any] = fields_raw if isinstance(fields_raw, list) else []
    rows_raw = data.get("data")
    if rows_raw is not None and not isinstance(rows_raw, list):
        raise RuntimeError(f"topic data 容器非 list: {type(rows_raw).__name__}")
    rows: list[Any] = rows_raw if isinstance(rows_raw, list) else []
    if not rows:
        return []
    converted: list[dict[str, Any]] = []
    rids: list[Any] = row_ids(data)
    for i, r in enumerate(rows):
        if isinstance(r, dict):
            if "fields" in r or "record" in r:
                fds = r.get("fields") or r.get("record") or {}
                rid = r.get("record_id") or r.get("id") or r.get("recordId") or (rids[i] if i < len(rids) else "")
                converted.append(
                    {
                        "record_id": rid,
                        "fields": fds,
                        **{
                            k: v
                            for k, v in r.items()
                            if k not in ("fields", "record", "record_id", "recordId", "id")
                        },
                    }
                )
            else:
                rid = r.get("record_id") or r.get("id") or (rids[i] if i < len(rids) else "")
                converted.append({"record_id": rid, "fields": r})
        elif isinstance(r, list) and fields:
            d = {fields[idx]: r[idx] for idx in range(min(len(fields), len(r)))}
            rid = rids[i] if i < len(rids) else ""
            converted.append({"record_id": rid, "fields": d})
        else:
            converted.append({"record_id": rids[i] if i < len(rids) else "", "fields": {}})
    return converted
