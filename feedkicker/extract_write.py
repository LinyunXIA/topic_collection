"""F31 选题写入：字段映射 + 「资讯链接 OR 话题名称」双键去重 + 批量落表（DESIGN §25.5/#251）。"""

from __future__ import annotations

import logging
from typing import Any

from feedkicker import bitable_lark
from feedkicker.extract_parse import _str_list, md_link_tokens, topic_key
from feedkicker.fetch import dedup_key
from feedkicker.topic_records import _extract_records

log = logging.getLogger(__name__)


def link_keys(raw: Any) -> set[str]:
    """把表内/候选的 `资讯链接` 原值归一为去重键集合（`existing_index`/`plan_writes` 共用）。

    真跑表内值常是 markdown 包裹 + 换行拼接 + tracking 参数的单字符串（旧 `canonicalize(str)`
    永不命中，skipped=0 已证）：目标 URL 由 `md_link_tokens` 按括号平衡扫描（任意嵌套，#N4）、
    标签不当 URL（#270）、相邻/混排各取各（#288）、非 URL token 过滤（#329）→ `dedup_key`
    统一去 tracking（`utm_*` 与常见广告参数）并保留有意义 query（#325）；详见 DESIGN §25.5。
    """
    keys: set[str] = set()
    for item in _str_list(raw):
        for url in md_link_tokens(item):
            if key := dedup_key(url):
                keys.add(key)
    return keys


def existing_index_full(
    app_token: str, table_id: str
) -> tuple[set[str], set[str], list[str]]:
    """分页拉目标表索引 `(归一话题名集合, 归一链接集合, 原始话题名列表)`；失败/容器异常 raise。

    前两项供双键去重（#251）；原始话题名（按归一键去重、保序）供提炼提示词注入
    「已有话题（避免重复）」清单，拦概念重复（同工具仅版本/角度不同、链接不同，#392）。
    响应兼容 records/items 与 fields+data 行式；两者皆非（如 rc0 的 `{}`）→ raise
    （不可识别响应不得静默空集，#265）；fields+data 缺「话题名称」列 → raise（PRV-4）；
    翻页走 offset + 页指纹守卫（#245）。
    """
    names: set[str] = set()
    links: set[str] = set()
    raw_names: list[str] = []
    offset = 0
    prev_fp = ""
    while True:
        bitable_lark._guard_offset(offset)
        bitable_lark.guard_pages(offset // bitable_lark._CHUNK + 1)
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
        fields_list = isinstance(fields_raw, list)
        has_rec = isinstance(data.get("records"), list) or isinstance(data.get("items"), list)
        if not (has_rec or (fields_list and isinstance(data.get("data"), list))):
            raise RuntimeError(f"选题表响应无法识别（无 records/items 或 fields+data 容器），中止写入以避免重复行: {str(data)[:200]}")
        if not (data.get("records") or data.get("items")) and fields_list and data.get("data") and "话题名称" not in fields_raw:
            raise RuntimeError("选题表响应为 fields+data 形态但缺「话题名称」列（含空 fields 有数据行），中止写入以避免重复行")
        records = _extract_records(data)
        for rec in records:
            fields = rec.get("fields") or {}
            fields = fields if isinstance(fields, dict) else {}
            for name in _str_list(fields.get("话题名称")):
                key = topic_key(name)
                if key and key not in names:
                    raw_names.append(name)
                names.add(key)
            links |= link_keys(fields.get("资讯链接"))
        if len(records) < bitable_lark._CHUNK:
            break
        offset += bitable_lark._CHUNK
    return names, links, raw_names


def existing_index(app_token: str, table_id: str) -> tuple[set[str], set[str]]:
    """双键去重索引（归一话题名, 归一链接）；包装 `existing_index_full`，兼容既有调用点。"""
    names, links, _ = existing_index_full(app_token, table_id)
    return names, links


def build_record(
    topic: dict[str, Any], provider_label: str, run_date: str, status: str = "未讨论"
) -> dict[str, Any]:
    """字段映射：LLM 三字段原样、链接/来源换行拼接、提炼日期/讨论状态/提取工具固定值。

    **select 字段一律写单元素数组**（lark-cli Tips：CellValue 恒为数组，写字符串被拒
    800030005）；取值须为表内已有选项（`讨论状态` 4 项、`提取工具` `MMax`/`DS`），写表外
    新值被拒 Provide an existing option value；不复用 `+field-list` 元数据判形态（真跑已证伪）。
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


def plan_writes(
    topics: list[dict[str, Any]],
    provider_label: str,
    run_date: str,
    existing_names: set[str],
    existing_links: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """去重规划：返回 (将写入记录, 将跳过记录)，`write_topics` 与 dry-run 打印共用（标注/计数/写入同源）。

    双键：话题名归一或任一 `资讯链接` 归一命中表内/本批已见即跳过；跳过项也须并入本批 seen 集合（否则按名跳过后同链接会误写，#293）。
    """
    picked: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    seen_links: set[str] = set()
    for topic in topics:
        key = topic_key(topic.get("话题名称"))
        links = link_keys(topic.get("资讯链接"))
        hit = (
            not key
            or key in existing_names
            or key in seen_names
            or bool(links & existing_links)
            or bool(links & seen_links)
        )
        if key:
            seen_names.add(key)
        seen_links |= links
        (skipped if hit else picked).append(build_record(topic, provider_label, run_date))
    return picked, skipped


def write_topics(
    app_token: str,
    table_id: str,
    topics: list[dict[str, Any]],
    provider_label: str,
    run_date: str,
    index: tuple[set[str], set[str]] | None = None,
) -> tuple[int, int, int]:
    """按双键查重跳过（幂等）后真写；dry-run 由 `extract_flow` 走 `existing_index`+`plan_writes`（#283）。

    `index` 可传入编排层为提示词注入而提前拉取的 `(names, links)`，避免 apply 路径重复分页；
    真写走 +record-batch-create ≤200/批；块失败 WARNING 后继续，返回 `(written, skipped, failed)`
    （failed = picked − written，使 summary 的 written+skipped+failed == topics，#298）。
    """
    if not app_token or not table_id:
        raise RuntimeError("写入选题表需要 app_token 与 table_id（salon 配置段）")
    existing_names, existing_links = index if index is not None else existing_index(app_token, table_id)
    picked, skipped = plan_writes(topics, provider_label, run_date, existing_names, existing_links)
    written = 0
    for i in range(0, len(picked), bitable_lark._CHUNK):
        chunk = picked[i : i + bitable_lark._CHUNK]
        payload = {"create_records": chunk}
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
    return written, len(skipped), len(picked) - written
