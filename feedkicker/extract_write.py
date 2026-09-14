"""F31 选题写入：字段映射 + 「资讯链接 OR 话题名称」双键去重 + 批量落表（DESIGN §25.5/#251）。"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from feedkicker import bitable_lark
from feedkicker.extract_parse import _str_list, topic_key
from feedkicker.fetch import canonicalize
from feedkicker.topic_records import _extract_records

log = logging.getLogger(__name__)

_MD_LINK = re.compile(r"\[(?P<inner>.*?)\]\((?P<target>.*?)\)", re.DOTALL)
_TRACKING = {"spm", "from", "fbclid", "gclid", "ref", "ref_src", "source", "mc_cid", "mc_eid"}


def _link_key(url: str) -> str:
    """单 URL 去重键：`canonicalize`（去 fragment/host 小写）后再剥 tracking 参数。"""
    canon = canonicalize(url).strip()
    if not canon:
        return ""
    parts = urlsplit(canon)
    if not parts.query:
        return canon
    try:
        pairs = parse_qsl(parts.query, keep_blank_values=True)
    except ValueError:
        return canon
    kept = sorted(
        (k, v)
        for k, v in pairs
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ""))


def link_keys(raw: Any) -> set[str]:
    """把表内/候选的 `资讯链接` 原值归一为去重键集合（`existing_index`/`plan_writes` 共用）。

    真跑表内值常是 markdown 包裹 + 换行拼接 + tracking 参数的单字符串（旧 `canonicalize(str)`
    永不命中，skipped=0 已证）：**只取 markdown 目标 URL**（`[标签](url)` 不得把标签当 URL，
    相邻多链接各取各，#270）→ 对「移除 markdown 片段后的余文」按空白拆，两者取并集
    （混合 markdown + 裸链时裸链不丢，#288）→ `_link_key` 去 tracking（`utm_*` 与常见
    广告参数）并保留有意义 query；详见 DESIGN §25.5。
    """
    keys: set[str] = set()
    for item in _str_list(raw):
        urls = _MD_LINK.sub(lambda m: f" {m.group('target')} ", item).split()
        for url in urls:
            if key := _link_key(url):
                keys.add(key)
    return keys


def existing_index(app_token: str, table_id: str) -> tuple[set[str], set[str]]:
    """分页拉目标表已有索引 `(归一话题名集合, 归一链接集合)`；拉取失败/容器异常 raise（不静默空集）。

    双键之因：LLM 命名非确定性，仅按名去重会漏判重复落表（真跑已证），并列按 `资讯链接`
    兜底。响应兼容 records/items 与 fields+data 行式；两者皆非（如 rc0 的 `{}`）→ raise
    （不可识别响应不得静默空集，#265）；fields+data 缺「话题名称」列 → raise（PRV-4）；
    翻页走 offset + 页指纹守卫（#245）。
    """
    names: set[str] = set()
    links: set[str] = set()
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
        if not (data.get("records") or data.get("items")) and fields_list and fields_raw and "话题名称" not in fields_raw:
            raise RuntimeError("选题表响应为 fields+data 形态但缺「话题名称」列，中止写入以避免重复行")
        records = _extract_records(data)
        for rec in records:
            fields = rec.get("fields") or {}
            for name in _str_list(fields.get("话题名称")):
                names.add(topic_key(name))
            links |= link_keys(fields.get("资讯链接"))
        if len(records) < bitable_lark._CHUNK:
            break
        offset += bitable_lark._CHUNK
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

    双键：话题名归一或任一 `资讯链接` 归一命中表内/本批已见即跳过；两者皆无才写（本批内同链接亦跳过）。
    """
    picked: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    seen_links: set[str] = set()
    for topic in topics:
        key = topic_key(topic.get("话题名称"))
        links = link_keys(topic.get("资讯链接"))
        if (
            not key
            or key in existing_names
            or key in seen_names
            or bool(links & existing_links)
            or bool(links & seen_links)
        ):
            skipped.append(build_record(topic, provider_label, run_date))
            continue
        seen_names.add(key)
        seen_links |= links
        picked.append(build_record(topic, provider_label, run_date))
    return picked, skipped


def write_topics(
    app_token: str,
    table_id: str,
    topics: list[dict[str, Any]],
    provider_label: str,
    run_date: str,
) -> tuple[int, int]:
    """按双键查重跳过（幂等）后真写；dry-run 由 `extract_flow` 走 `existing_index`+`plan_writes`（#283）。

    真写走 +record-batch-create ≤200/批；块失败 WARNING 后继续，返回实际成功数。
    """
    if not app_token or not table_id:
        raise RuntimeError("写入选题表需要 app_token 与 table_id（salon 配置段）")
    existing_names, existing_links = existing_index(app_token, table_id)
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
    return written, len(skipped)
