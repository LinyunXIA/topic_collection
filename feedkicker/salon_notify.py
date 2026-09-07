"""salon Wiki 大纲卡片推送：连败计数与 3 连败纯文本 SOS（对齐 push.py）。"""

from __future__ import annotations

import json
import logging
from typing import Any

from feedkicker import feishu, store

log = logging.getLogger(__name__)

SALON_FAIL_STREAK_KEY = "salon_fail_streak"
SOS_THRESHOLD = 3


def send_wiki_card(
    cfg: Any, conn: Any, wiki_urls: list[str], dry_run: bool = False
) -> bool:
    """推送 Wiki 大纲汇总卡片。

    失败时 strip_actions 降级为纯链接卡片重试一次；仍败则 meta 连败 +1，
    达 SOS_THRESHOLD 且 webhook 非空时发纯文本求救并清零。dry-run 只打印 payload。
    返回 True 表示送达（或 dry-run / 无链接），False 表示最终失败。
    """
    if not wiki_urls:
        return True
    payload = feishu.build_card([], 0, [], wiki_urls=wiki_urls)
    if dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        log.info("dry-run 卡片预览已打印（含 %d 个 Wiki 链接）", len(wiki_urls))
        return True
    try:
        ok = feishu.send(
            payload,
            cfg.feishu_webhook,
            cfg.http.timeout_seconds,
            cfg.http.user_agent,
            secret=cfg.feishu_secret,
        )
        if not ok:
            log.warning("Wiki 卡片发送失败，降级为纯链接卡片重试一次")
            ok = feishu.send(
                feishu.strip_actions(payload),
                cfg.feishu_webhook,
                cfg.http.timeout_seconds,
                cfg.http.user_agent,
                secret=cfg.feishu_secret,
            )
        if ok:
            streak = int(store.get_meta(conn, SALON_FAIL_STREAK_KEY, "0"))
            if streak:
                log.info("Wiki 卡片推送恢复，清零连败计数（此前 %d 次）", streak)
            store.set_meta(conn, SALON_FAIL_STREAK_KEY, "0")
            log.info("Wiki 卡片已推送 %d 个链接", len(wiki_urls))
            return True
        streak = int(store.get_meta(conn, SALON_FAIL_STREAK_KEY, "0")) + 1
        store.set_meta(conn, SALON_FAIL_STREAK_KEY, str(streak))
        log.warning("Wiki 卡片推送失败，连败 %d 次", streak)
        if streak >= SOS_THRESHOLD and cfg.feishu_webhook:
            sos = (
                f"⚠️ feedkicker salon 连续 {streak} 次 Wiki 大纲卡片推送失败，"
                f"请检查机器人状态/网络。最近一班 {len(wiki_urls)} 份大纲 Wiki 已建成但卡片可能未送达。"
            )
            feishu.send_text(
                sos,
                cfg.feishu_webhook,
                cfg.http.timeout_seconds,
                cfg.http.user_agent,
                secret=cfg.feishu_secret,
            )
            store.set_meta(conn, SALON_FAIL_STREAK_KEY, "0")
        return False
    except Exception as e:  # noqa: BLE001
        log.warning("Wiki 卡片推送异常: %s", e)
        return False
