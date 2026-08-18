"""HTML→Markdown 清洗 + 语言检测 — DESIGN §2/§6

CPU 密集（trafilatura/selectolax/lingua）一律 asyncio.to_thread。
cleaner 阶段标记 unparseable 文章、跳过 LLM 入队。
"""

from __future__ import annotations

import asyncio
import logging
import re

import trafilatura
from lingua import LanguageDetectorBuilder

logger = logging.getLogger(__name__)

# ── 语言检测器（单例，启动时构建） ────────────────────────────────
_detector = None


def _get_detector():
    """延迟构建 lingua 语言检测器。"""
    global _detector
    if _detector is None:
        _detector = LanguageDetectorBuilder.from_all_languages().build()
    return _detector


# ── HTML→Markdown ─────────────────────────────────────────────────

def _extract_content_sync(html: str) -> tuple[str, str]:
    """同步提取：HTML → (content_md, content_text)。

    trafilatura 负责主内容提取 + Markdown 转换。
    """
    # Markdown 版本
    md = trafilatura.extract(
        html,
        output_format="markdown",
        include_links=True,
        include_images=False,
        no_fallback=False,
    ) or ""

    # 纯文本版本
    text = trafilatura.extract(
        html,
        output_format="txt",
        include_links=False,
        no_fallback=False,
    ) or ""

    return md.strip(), text.strip()


async def extract_content(html: str) -> tuple[str, str]:
    """异步提取：HTML → (content_md, content_text)。"""
    return await asyncio.to_thread(_extract_content_sync, html)


def _detect_language_sync(text: str) -> str:
    """同步检测语言（lingua），返回 ISO 639-1 代码。"""
    if not text or len(text.strip()) < 10:
        return "unknown"
    detector = _get_detector()
    try:
        lang = detector.detect_language_of(text.strip())
        if lang is None:
            return "unknown"
        return lang.iso_code_639_1.name.lower()
    except Exception:
        return "unknown"


async def detect_language(text: str) -> str:
    """异步检测语言。"""
    return await asyncio.to_thread(_detect_language_sync, text)


def _clean_html_sync(html: str, max_bytes: int = 5_242_880) -> str:
    """同步清洗 HTML：去 script/style + 截断。"""
    if not html:
        return ""
    # 截断超大 HTML
    html_bytes = html.encode("utf-8", errors="ignore")
    if len(html_bytes) > max_bytes:
        html = html_bytes[:max_bytes].decode("utf-8", errors="ignore")
        logger.warning("HTML 截断至 %d bytes", max_bytes)

    # 去 script/style 标签
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    return html


async def clean_article(html: str, title: str = "", max_bytes: int = 5_242_880) -> dict:
    """完整清洗流程：HTML → Markdown + 纯文本 + 语言检测。

    Returns:
        {
            "content_md": str,
            "content_text": str,
            "lang": str,
            "word_count": int,
            "is_parseable": bool,
        }
    """
    cleaned_html = await asyncio.to_thread(_clean_html_sync, html, max_bytes)
    content_md, content_text = await extract_content(cleaned_html)

    # 如果提取结果为空，尝试用标题+原始文本
    if not content_text and title:
        content_text = title
        content_md = f"# {title}"

    is_parseable = bool(content_text.strip())

    lang = "unknown"
    word_count = 0
    if is_parseable:
        lang = await detect_language(content_text)
        word_count = len(content_text.split())

    return {
        "content_md": content_md,
        "content_text": content_text,
        "lang": lang,
        "word_count": word_count,
        "is_parseable": is_parseable,
    }
