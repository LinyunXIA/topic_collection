"""去重 — URL hash + content hash（DESIGN §6）

url_hash = sha256(canonical_url)
content_hash = sha256(cleaned_text)
LLM 之前的快速精确去重；跨源近似去重（向量 cosine）在 embed_core 后触发。
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse, urlunparse


def canonicalize_url(url: str) -> str:
    """URL 规范化：去 fragment、去 trailing slash、lowercase scheme+host。"""
    parsed = urlparse(url)
    # 去 fragment、统一 scheme/host 小写
    canonical = parsed._replace(
        fragment="",
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
    )
    path = canonical.path.rstrip("/") or "/"
    return urlunparse(canonical._replace(path=path))


def url_hash(url: str) -> str:
    """sha256(canonical_url) — 用于 articles.url_hash 唯一索引。"""
    canonical = canonicalize_url(url)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def content_hash(text: str) -> str:
    """sha256(cleaned_text) — 用于 articles.content_hash + 版本守卫。"""
    # 归一化：去多余空白
    normalized = re.sub(r"\s+", " ", text.strip())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def is_same_content(hash1: str, hash2: str) -> bool:
    """判断两个 content hash 是否相同。"""
    return hash1 == hash2
