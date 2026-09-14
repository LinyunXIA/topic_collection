"""F32 dry-run 清单与运行统计输出（自 extract_flow 拆出，DESIGN §25.6/#252）。"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger(__name__)


def print_dry_run(planned: list[dict[str, Any]], skipped: list[dict[str, Any]]) -> None:
    """逐条打印清单（`[将写入]`/`[已存在跳过]` 前缀 + 可统计 JSON 行），不写表。

    入参来自 `extract_write.plan_writes`（与写入同一规划），故标注数量与 summary
    `pending`/`skipped` 必然一致。
    """
    print(f"待写选题 {len(planned)} 个（dry-run，未写表；目标表已存在跳过 {len(skipped)} 个）：")
    for i, record in enumerate(planned, 1):
        print(f"[将写入] {i}. {record['话题名称']}")
        print(json.dumps(record, ensure_ascii=False))
    for i, record in enumerate(skipped, 1):
        print(f"[已存在跳过] {i}. {record['话题名称']}")
        print(json.dumps(record, ensure_ascii=False))


def print_summary(stats: dict[str, Any]) -> None:
    print(json.dumps(stats, ensure_ascii=False))
    log.info(
        "提炼完成：模式=%s 批=%d 调用=%d 话题=%d 写入=%d 待写=%d 跳过=%d 写入失败=%d 失败批=%d 空批=%d",
        stats["mode"], stats["batches"], stats["llm_calls"], stats["topics"],
        stats["written"], stats["pending"], stats["skipped"], stats["failed_writes"],
        stats["failed_batches"], stats["empty_batches"],
    )
