"""飞书 webhook 推送：签名、HTTP 发送、纯文本 SOS。

卡片构建见 feedkicker.feishu_card；此处 facade re-export 保持
feishu.build_card / feishu.escape_inline / feishu.strip_actions 调用点不变。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any

import httpx

from feedkicker.feishu_card import (
    build_card as build_card,
)
from feedkicker.feishu_card import (
    escape_inline as escape_inline,
)
from feedkicker.feishu_card import (
    strip_actions as strip_actions,
)

log = logging.getLogger(__name__)


def gen_sign(timestamp: str, secret: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


def _post(
    payload: dict[str, Any],
    webhook_url: str,
    timeout_seconds: float,
    user_agent: str,
    secret: str = "",
) -> bool:
    if not webhook_url:
        log.warning("跳过推送：webhook 为空")
        return False
    try:
        body_payload = dict(payload)
        if secret:
            ts = str(int(time.time()))
            body_payload["timestamp"] = ts
            body_payload["sign"] = gen_sign(ts, secret)
        body = json.dumps(body_payload, ensure_ascii=False)
        resp = httpx.post(
            webhook_url,
            content=body.encode("utf-8"),
            timeout=timeout_seconds,
            headers={"Content-Type": "application/json", "User-Agent": user_agent},
        )
        if resp.status_code != 200:
            log.warning("推送失败：HTTP %d", resp.status_code)
            return False
        data = resp.json()
        code = data.get("StatusCode", data.get("code", 0))
        if code != 0:
            log.warning("推送被飞书拒绝，业务码非 0：%s", data)
            return False
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("推送异常: %s", e)
        return False


def send(
    payload: dict[str, Any],
    webhook_url: str,
    timeout_seconds: float,
    user_agent: str,
    secret: str = "",
) -> bool:
    return _post(payload, webhook_url, timeout_seconds, user_agent, secret)


def send_text(
    text_msg: str,
    webhook_url: str,
    timeout_seconds: float,
    user_agent: str,
    secret: str = "",
) -> bool:
    payload = {"msg_type": "text", "content": {"text": text_msg}}
    return _post(payload, webhook_url, timeout_seconds, user_agent, secret)
