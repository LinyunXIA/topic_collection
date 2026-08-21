"""飞书 Webhook 推送 — DESIGN §10.4 / PRD §15 #8

Webhook 机器人先行（open.feishu.cn），无需鉴权续期；失败仅 warning，不阻塞报告生成。
外发统一走 app/core/egress.safe_post 白名单校验。
"""

from __future__ import annotations

import logging

from app.core.egress import safe_post

logger = logging.getLogger(__name__)


async def send_feishu_markdown(webhook: str, title: str, markdown: str) -> bool:
    """推送 Markdown 到飞书群机器人。

    webhook: 完整 URL（含 token），来自环境变量 FEISHU_WEBHOOK
    title:   报告标题（如 "日报 2026-08-21"）
    markdown: 报告 Markdown 正文
    返回 True=成功，False=失败（已 warning，不抛）
    """
    if not webhook:
        logger.warning("飞书推送跳过：webhook 为空")
        return False

    # 飞书 Webhook 机器人支持 msg_type=text（最稳）或 post / interactive 卡片
    # 此处用 text，兼容性最佳；长文截断 30000 字符（飞书限制）
    body = markdown[:30000] if len(markdown) > 30000 else markdown
    payload = {
        "msg_type": "text",
        "content": {"text": f"{title}\n\n{body}"},
    }

    try:
        resp = await safe_post(webhook, json=payload, timeout=10.0)
        if resp.status_code != 200:
            logger.warning("飞书推送失败: HTTP %s %s", resp.status_code, resp.text[:500])
            return False
        # 飞书业务码：{"StatusCode":0} 或 {"code":0}
        try:
            data = resp.json()
            code = data.get("StatusCode", data.get("code", 0))
            if code != 0:
                logger.warning("飞书推送业务失败: %s", data)
                return False
        except Exception:
            pass
        logger.info("飞书推送成功: %s", title)
        return True
    except Exception as e:
        logger.warning("飞书推送异常: %s", e)
        return False
