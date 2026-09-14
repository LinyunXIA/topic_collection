"""F32 dry-run 清单与运行统计输出（自 extract_flow 拆出，DESIGN §25.6/#252）。"""

from __future__ import annotations

import json
import logging
from typing import Any

from feedkicker import extract_write

log = logging.getLogger(__name__)


def print_dry_run(topics: list[dict[str, Any]], provider_label: str, run_date: str, skipped: int) -> None:
    """逐条打印完整待写清单（人读行 + 可统计 JSON 行），不写表。"""
    print(f"待写选题 {len(topics)} 个（dry-run，未写表；目标表已存在跳过 {skipped} 个）：")
    for i, topic in enumerate(topics, 1):
        record = extract_write.build_record(topic, provider_label, run_date, "未讨论")
        print(f"{i}. {record['话题名称']}")
        print(json.dumps(record, ensure_ascii=False))


def print_summary(stats: dict[str, Any]) -> None:
    print(json.dumps(stats, ensure_ascii=False))
    log.info(
        "提炼完成：模式=%s 批=%d 调用=%d 话题=%d 写入=%d 待写=%d 跳过=%d 失败批=%d",
        stats["mode"], stats["batches"], stats["llm_calls"], stats["topics"],
        stats["written"], stats["pending"], stats["skipped"], stats["failed_batches"],
    )
