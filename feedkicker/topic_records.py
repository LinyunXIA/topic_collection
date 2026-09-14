"""topic 响应记录归一（自 topic.py 抽出以守住 ≤200 行门，DESIGN §21.2）。"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def _extract_records(data: Any) -> list[dict[str, Any]]:
    """记录提取：容器类型异常（顶层非 dict / records 非 list[dict]）按空页 + WARNING（#237）。

    响应兼容 records/items 包装与 data.fields+data.data 行式两种形态；
    坏容器一律不抛异常，交调用方按空页终止，避免下游 rec.get 崩。
    """
    if not isinstance(data, dict):
        log.warning("topic 响应顶层非对象（%s），按空页处理", type(data).__name__)
        return []
    records = data.get("records") or data.get("items")
    if records:
        if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
            log.warning("topic records 非 list[dict]（%s），按空页处理", type(records).__name__)
            return []
        return list(records)
    fields_raw = data.get("fields")
    fields: list[Any] = fields_raw if isinstance(fields_raw, list) else []
    rows_raw = data.get("data")
    if rows_raw is not None and not isinstance(rows_raw, list):
        log.warning("topic data 容器非 list（%s），按空页处理", type(rows_raw).__name__)
        return []
    rows: list[Any] = rows_raw if isinstance(rows_raw, list) else []
    if not rows:
        return []
    converted: list[dict[str, Any]] = []
    rids: list[Any] = (
        data.get("record_ids")
        or data.get("recordIds")
        or data.get("ids")
        or data.get("record_id_list")
        or data.get("recordId_list")
        or data.get("recordIdList")
        or []
    )
    for i, r in enumerate(rows):
        if isinstance(r, dict):
            if "fields" in r or "record" in r:
                fds = r.get("fields") or r.get("record") or {}
                rid = r.get("record_id") or r.get("id") or r.get("recordId") or (rids[i] if i < len(rids) else "")
                converted.append(
                    {
                        "record_id": rid,
                        "fields": fds,
                        **{k: v for k, v in r.items() if k not in ("fields", "record")},
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
