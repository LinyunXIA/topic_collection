"""共享出口白名单 — PRD §12 / DESIGN §10.4 / §5.4.1 2.0.5

飞书、外部 LLM 等外发流量统一走 safe_post / safe_get，白名单校验失败直接
抛 PermanentError（不重试），FEED_FETCH_ALLOW_ALL 时抓取可全量放行。
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

import httpx

from app.llm.client import PermanentError

logger = logging.getLogger(__name__)

# PRD §12 外发白名单（共享出口）
ALLOWED_HOSTS: set[str] = {
    "open.feishu.cn",
    "open.larksuite.com",
    "api.openai.com",
    "api.siliconflow.cn",
    "api.minimax.chat",
    "api.minimaxi.com",
    "api.anthropic.com",
    "ark.cn-beijing.volces.com",
}

# FEED_FETCH_ALLOW_ALL=1 时，RSS/API 抓取绕过白名单（仅抓取链路）
_FEED_ALLOW_ALL_ENV = "FEED_FETCH_ALLOW_ALL"


def _is_private_host(host: str) -> bool:
    """本地/私网地址视为非外发（本地推理、开发联调不算外发流量）。

    - localhost / 以 .localhost 结尾（含 127.0.0.1、::1）
    - IPv4/IPv6 环回、私网、链路本地（10./192.168./172.16-31./169.254. 等）
    hostname（非 IP）除 localhost 外不做私网判断，交给白名单。
    """
    if host == "" or host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        import ipaddress

        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private or ip.is_link_local
    except ValueError:
        return False


def _is_allowed(url: str, *, is_feed: bool = False) -> bool:
    """校验 url 是否在白名单内。"""
    if is_feed and os.getenv(_FEED_ALLOW_ALL_ENV) == "1":
        return True
    # 显式开关：开发期 FEED_FETCH_ALLOW_ALL 也可通过环境变量放行全部抓取域名
    # 但飞书/外部 LLM 推送始终校验，不受此开关影响（is_feed=False）
    try:
        host = urlparse(url).hostname or ""
    except Exception:
        return False
    host = host.lower()
    if _is_private_host(host):
        return True
    if host in ALLOWED_HOSTS:
        return True
    # 子域名放行（如 open.feishu.cn 的子域）
    for allowed in ALLOWED_HOSTS:
        if host == allowed or host.endswith("." + allowed):
            return True
    return False


def _ensure_allowed(url: str, *, is_feed: bool = False) -> None:
    if not _is_allowed(url, is_feed=is_feed):
        raise PermanentError(f"外发域名未在白名单: {url}（PRD §12，需经 app/core/egress.py 白名单）")


async def safe_post(
    url: str,
    *,
    json: dict | None = None,
    data: dict | None = None,
    headers: dict | None = None,
    timeout: float = 10.0,
    is_feed: bool = False,
    follow_redirects: bool = False,
) -> httpx.Response:
    """白名单校验后 POST，超时默认 10s。"""
    _ensure_allowed(url, is_feed=is_feed)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=follow_redirects) as client:
        resp = await client.post(url, json=json, data=data, headers=headers)
        return resp


async def safe_get(
    url: str,
    *,
    headers: dict | None = None,
    params: dict | None = None,
    timeout: float = 10.0,
    is_feed: bool = False,
    follow_redirects: bool = False,
) -> httpx.Response:
    """白名单校验后 GET。"""
    _ensure_allowed(url, is_feed=is_feed)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=follow_redirects) as client:
        resp = await client.get(url, headers=headers, params=params)
        return resp
