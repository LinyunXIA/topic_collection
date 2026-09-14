"""F31 选题写入：字段映射 + 「资讯链接 OR 话题名称」双键去重 + 批量落表（DESIGN §25.5/#251）。"""

from __future__ import annotations

import logging
from typing import Any

from feedkicker import bitable_lark
from feedkicker.extract_parse import _str_list, topic_key
from feedkicker.fetch import canonicalize
from feedkicker.topic_records import _extract_records

log = logging.getLogger(__name__)


def existing_index(app_token: str, table_id: str) -> tuple[set[str], set[str]]:
    """分页拉目标表已有索引 `(归一话题名集合, 归一链接集合)`；拉取失败/容器异常 raise（不静默空集）。

    双键去重之因：LLM 命名非确定性——同一新闻重跑会产出不同「话题名称」，仅按名去重会
    漏判并重复落表（真跑已证）；故并列按 `资讯链接` 兜底。名称归一沿用 `topic_key`
    （NFKC+strip+casefold），链接归一用 `feedkicker.fetch.canonicalize`（去 fragment、
    host 小写、保留 query）。
    响应兼容 records/items 包装与 fields+data 行式（topic_records._extract_records 归一）；
    fields+data 形态缺「话题名称」列 → raise 中止（不得静默空集，PRV-4）。
    翻页走 offset 兜底 + 页指纹守卫（#245）。
    """
    names: set[str] = set()
    links: set[str] = set()
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
                "--field-id", "资讯链接",
                "--limit", "200",
                "--offset", str(offset),
                "--json",
            ],
            timeout=120,
        )
        if not bitable_lark._ok(proc):
            raise RuntimeError("拉取选题表已有「话题名称/资讯链接」失败，中止写入以避免重复行")
        data = bitable_lark._data(proc)
        if not isinstance(data, dict):
            raise RuntimeError(f"选题表响应不是 JSON 对象: {str(data)[:200]}")
        prev_fp = bitable_lark._page_guard(prev_fp, data)
        fields_raw = data.get("fields")
        if (
            not (data.get("records") or data.get("items"))
            and isinstance(fields_raw, list)
            and fields_raw
            and "话题名称" not in fields_raw
        ):
            raise RuntimeError("选题表响应为 fields+data 形态但缺「话题名称」列，中止写入以避免重复行")
        records = _extract_records(data)
        for rec in records:
            fields = rec.get("fields") or {}
            for name in _str_list(fields.get("话题名称")):
                names.add(topic_key(name))
            for link in _str_list(fields.get("资讯链接")):
                key = canonicalize(link).strip()
                if key:
                    links.add(key)
        if len(records) < bitable_lark._CHUNK:
            break
        offset += bitable_lark._CHUNK
    return names, links


def build_record(
    topic: dict[str, Any], provider_label: str, run_date: str, status: str = "未讨论"
) -> dict[str, Any]:
    """字段映射：LLM 三字段原样、链接/来源换行拼接、提炼日期/讨论状态/提取工具固定值。

    **select 字段一律写数组**（`讨论状态` / `提取工具` 均为单选 select，写单元素数组）：
    lark-cli `base +record-batch-create --help` Tips 明确 select CellValue 恒为数组
    （`multiple=false` 时也须单元素数组，形如 `"select": ["Todo"]`），写字符串会被
    服务端拒（800030005 not_found）。取值必须是**表内已有选项**：`讨论状态`
    为 `未讨论/已选题/不选择/待继续评估`，`提取工具` 为 `MMax`/`DS`（`飞书` 留给人工
    路径，不在 provider 注册表引入）；写表外新值会被拒 `800030005 Provide an existing
    option value`。不复用 `+field-list` 元数据判形态（真跑已证伪）。
    """
    return {
        "话题名称": str(topic.get("话题名称") or "").strip(),
        "可使用工具": str(topic.get("可使用工具") or ""),
        "相关AI原理": str(topic.get("相关AI原理") or ""),
        "资讯链接": "\n".join(_str_list(topic.get("资讯链接"))),
        "出处来源": "\n".join(_str_list(topic.get("出处来源"))),
        "提炼日期": run_date,
        "讨论状态": [status],
        "提取工具": [provider_label],
    }


def write_topics(
    app_token: str,
    table_id: str,
    topics: list[dict[str, Any]],
    provider_label: str,
    run_date: str,
    dry_run: bool = False,
) -> tuple[int, int]:
    """按「资讯链接 OR 话题名称」双键查重跳过（幂等）；dry_run 仅返回 (待写数, 跳过数)，零写调用。

    双键是 LLM 命名非确定性下的真幂等兜底：话题名归一命中，或该 topic 任一 `资讯链接`
    经 `canonicalize` 归一后命中表内/本批已见链接 → 跳过；两者皆无才写。本批内同样按
    name 或 link 任一已见即跳过，避免同链接在批内重复落表。
    真写走 +record-batch-create ≤200/批；块失败 WARNING 后继续，返回实际成功数。
    """
    if not app_token or not table_id:
        raise RuntimeError("写入选题表需要 app_token 与 table_id（salon 配置段）")
    existing_names, existing_links = existing_index(app_token, table_id)
    picked: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    seen_links: set[str] = set()
    skipped = 0
    for topic in topics:
        key = topic_key(topic.get("话题名称"))
        links = {canonicalize(link).strip() for link in _str_list(topic.get("资讯链接"))}
        links.discard("")
        if (
            not key
            or key in existing_names
            or key in seen_names
            or bool(links & existing_links)
            or bool(links & seen_links)
        ):
            skipped += 1
            continue
        seen_names.add(key)
        seen_links |= links
        picked.append(topic)
    if dry_run:
        return len(picked), skipped
    written = 0
    for i in range(0, len(picked), bitable_lark._CHUNK):
        chunk = picked[i : i + bitable_lark._CHUNK]
        payload = {"create_records": [build_record(t, provider_label, run_date) for t in chunk]}
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
