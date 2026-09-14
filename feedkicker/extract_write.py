"""F31 选题写入：字段映射 + 按「话题名称」去重 + 批量落表（DESIGN §25.5/#251）。"""

from __future__ import annotations

import logging
from typing import Any

from feedkicker import bitable_lark
from feedkicker.extract_parse import _str_list
from feedkicker.topic_records import _extract_records

log = logging.getLogger(__name__)


def existing_topics(app_token: str, table_id: str) -> set[str]:
    """分页拉目标表「话题名称」集合；拉取失败/容器异常 raise（不静默空集，避免重复写入）。

    响应兼容 records/items 包装与 fields+data 行式（topic_records._extract_records 归一）；
    翻页走 offset 兜底 + 页指纹守卫（#245）。
    """
    names: set[str] = set()
    offset = 0
    prev_fp = ""
    while True:
        bitable_lark._guard_offset(offset)
        proc = bitable_lark._run(
            [
                "base", "+record-list",
                "--base-token", app_token,
                "--table-id", table_id,
                "--field-id", "话题名称",
                "--limit", "200",
                "--offset", str(offset),
                "--json",
            ],
            timeout=120,
        )
        if not bitable_lark._ok(proc):
            raise RuntimeError("拉取选题表已有「话题名称」失败，中止写入以避免重复行")
        data = bitable_lark._data(proc)
        if not isinstance(data, dict):
            raise RuntimeError(f"选题表响应不是 JSON 对象: {str(data)[:200]}")
        prev_fp = bitable_lark._page_guard(prev_fp, data)
        records = _extract_records(data)
        for rec in records:
            for name in _str_list((rec.get("fields") or {}).get("话题名称")):
                names.add(name)
        if len(records) < bitable_lark._CHUNK:
            break
        offset += bitable_lark._CHUNK
    return names


def resolve_status_value(
    app_token: str, table_id: str, field: str = "讨论状态", option: str = "未讨论"
) -> str | list[str]:
    """按字段元数据 `multiple`/类型决定 select 选项写入形态：单选 str、多选 [option]（PRV-1）。

    权威口径是 `base +field-list` 元数据（multiple:true / 多选码 4）；lark-cli 行式渲染
    把 select 值显示成数组，不代表字段是多选。元数据读取失败/字段缺失 → 保守按字符串
    写入并 WARNING（真多选才会被服务端拒绝、真单选绝不会因数组被拒）。
    """
    try:
        proc = bitable_lark._run(
            [
                "base", "+field-list",
                "--base-token", app_token,
                "--table-id", table_id,
                "--json",
            ],
            timeout=60,
        )
        if not bitable_lark._ok(proc):
            raise RuntimeError("field-list 业务失败")
        data = bitable_lark._data(proc)
        fields = data.get("fields") or data.get("items") or []
        for f in fields:
            if not isinstance(f, dict):
                continue
            name = str(f.get("field_name") or f.get("name") or "")
            if name != field:
                continue
            ftype = str(f.get("type") or f.get("field_type") or "").strip().lower()
            if f.get("multiple") is True or ftype in ("4", "multiselect", "multiple_select"):
                return [option]
            return option
        raise RuntimeError(f"字段未找到: {field}")
    except Exception as e:  # noqa: BLE001
        log.warning("%s 字段元数据读取失败，按单选字符串写入兜底: %s", field, e)
        return option


def build_record(
    topic: dict[str, Any], provider_label: str, run_date: str, status_value: str | list[str]
) -> dict[str, Any]:
    """字段映射：LLM 三字段原样、链接/来源换行拼接、提炼日期/讨论状态/提取工具固定值。"""
    return {
        "话题名称": str(topic.get("话题名称") or "").strip(),
        "可使用工具": str(topic.get("可使用工具") or ""),
        "相关AI原理": str(topic.get("相关AI原理") or ""),
        "资讯链接": "\n".join(_str_list(topic.get("资讯链接"))),
        "出处来源": "\n".join(_str_list(topic.get("出处来源"))),
        "提炼日期": run_date,
        "讨论状态": status_value,
        "提取工具": provider_label,
    }


def write_topics(
    app_token: str,
    table_id: str,
    topics: list[dict[str, Any]],
    provider_label: str,
    run_date: str,
    dry_run: bool = False,
) -> tuple[int, int]:
    """按「话题名称」查重跳过（幂等）；dry_run 仅返回 (待写数, 跳过数)，零写调用。

    真写走 +record-batch-create ≤200/批；块失败 WARNING 后继续，返回实际成功数。
    """
    if not app_token or not table_id:
        raise RuntimeError("写入选题表需要 app_token 与 table_id（salon 配置段）")
    existing = existing_topics(app_token, table_id)
    picked: list[dict[str, Any]] = []
    seen: set[str] = set()
    skipped = 0
    for topic in topics:
        name = str(topic.get("话题名称") or "").strip()
        if not name or name in existing or name in seen:
            skipped += 1
            continue
        seen.add(name)
        picked.append(topic)
    if dry_run:
        return len(picked), skipped
    status_value = resolve_status_value(app_token, table_id)
    written = 0
    for i in range(0, len(picked), bitable_lark._CHUNK):
        chunk = picked[i : i + bitable_lark._CHUNK]
        payload = {"create_records": [build_record(t, provider_label, run_date, status_value) for t in chunk]}
        with bitable_lark._json_arg(payload) as (jflag, jval):
            proc = bitable_lark._run(
                [
                    "base", "+record-batch-create",
                    "--base-token", app_token,
                    "--table-id", table_id,
                    jflag, jval,
                ],
                timeout=300,
            )
        if not bitable_lark._ok(proc):
            log.warning("选题批量写入失败（第 %d 批 %d 条）", i // bitable_lark._CHUNK + 1, len(chunk))
            continue
        written += len(chunk)
    return written, skipped
