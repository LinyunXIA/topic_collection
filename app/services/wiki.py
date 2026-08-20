"""Wiki 词条生成 — DESIGN §6/§14

Phase 1：文章词条生成（related_json = 同主题 article top-5）。
P2：实体词条 + 交叉链接。
"""

from __future__ import annotations

import json
import logging
import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def _slugify(title: str, ref_id: int | None = None) -> str:
    """标题 → URL-friendly slug。

    wiki_pages.slug 是 UNIQUE，而标题重复很常见（多家媒体转同一篇通稿、
    "本周简报" 这类固定标题、纯符号标题 slug 化后为空串）。不带 ref_id 时
    第二篇会 upsert 覆盖第一篇的正文，而 ref_id 仍指向第一篇——内容与引用错位。
    因此附加 ref_id 后缀保证一文一条。
    """
    slug = title.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = slug[:100].strip("-")
    if ref_id is None:
        return slug
    return f"{slug}-{ref_id}" if slug else f"article-{ref_id}"


async def generate_article_wiki(
    session: AsyncSession,
    article_id: int,
    settings,
) -> int | None:
    """为文章生成 wiki 词条（DESIGN §6：摘要落地后触发）。

    Phase 1: related_json = 同主题 article top-5（按 score DESC, published_at DESC）。
    返回 wiki_page id。
    """
    # 获取文章信息 + 摘要
    result = await session.execute(
        text(
            "SELECT a.title, a.content_text, a.source_url, "
            "  s.summary_text, s.key_points_json "
            "FROM articles a "
            "LEFT JOIN summaries s ON s.article_id = a.id AND s.lang = 'zh' "
            "WHERE a.id = :aid"
        ),
        {"aid": article_id},
    )
    row = result.mappings().first()
    if not row or not row["title"]:
        return None

    title = row["title"]
    summary = row["summary_text"] or ""
    key_points = row["key_points_json"] or []
    if isinstance(key_points, str):
        key_points = json.loads(key_points)

    # 构造 Markdown 内容
    md_parts = [f"# {title}\n"]
    if summary:
        md_parts.append(f"{summary}\n")
    if key_points:
        md_parts.append("## 要点\n")
        for kp in key_points:
            md_parts.append(f"- {kp}")
        md_parts.append("")
    if row["source_url"]:
        md_parts.append(f"原文: {row['source_url']}")

    content_md = "\n".join(md_parts)

    # 获取 related articles（同主题 top-5，DESIGN §6 Phase 1）
    related_result = await session.execute(
        text(
            # 共享多个主题的文章会被 JOIN 出多行，必须按文章聚合去重，
            # 否则同一篇相关文章会在 top-5 里重复占位
            "SELECT a.id, a.title, MAX(at.score) AS best_score, "
            "       MAX(a.published_at) AS pub "
            "FROM article_topics at "
            "JOIN articles a ON a.id = at.article_id "
            "WHERE a.id != :aid AND a.dedupe_of IS NULL "
            "AND at.topic_id IN ("
            "  SELECT topic_id FROM article_topics WHERE article_id = :aid"
            ") "
            "GROUP BY a.id, a.title "
            "ORDER BY best_score DESC, pub DESC "
            "LIMIT 5"
        ),
        {"aid": article_id},
    )
    related = [{"id": r["id"], "title": r["title"]} for r in related_result.mappings().all()]

    # Upsert wiki_page
    slug = _slugify(title, article_id)
    await session.execute(
        text(
            "INSERT INTO wiki_pages (kind, ref_id, title, slug, content_md, related_json) "
            "VALUES ('article', :ref_id, :title, :slug, :content, :related) "
            "ON CONFLICT (slug) DO UPDATE "
            "SET ref_id = EXCLUDED.ref_id, "
            "    title = EXCLUDED.title, "
            "    content_md = EXCLUDED.content_md, "
            "    related_json = EXCLUDED.related_json, "
            "    updated_at = now()"
        ),
        {
            "ref_id": article_id,
            "title": title,
            "slug": slug,
            "content": content_md,
            "related": json.dumps(related, ensure_ascii=False),
        },
    )

    result = await session.execute(
        text("SELECT id FROM wiki_pages WHERE slug = :slug"), {"slug": slug}
    )
    row = result.first()
    wiki_id = row[0] if row else None
    logger.info("generate_article_wiki: article=%d → wiki=%s", article_id, wiki_id)
    return wiki_id


async def search_wiki(
    session: AsyncSession,
    query: str,
    limit: int = 20,
) -> list[dict]:
    """搜索 wiki 词条（关键词全文搜索，PRD §15 验收 5）。"""
    result = await session.execute(
        text(
            "SELECT id, kind, ref_id, title, slug, content_md "
            "FROM wiki_pages "
            "WHERE title ILIKE :q OR content_md ILIKE :q "
            "LIMIT :limit"
        ),
        {"q": f"%{query}%", "limit": limit},
    )
    return [dict(row) for row in result.mappings().all()]


async def get_wiki_page(session: AsyncSession, slug: str) -> dict | None:
    """获取 wiki 词条。"""
    result = await session.execute(
        text(
            "SELECT id, kind, ref_id, title, slug, content_md, related_json "
            "FROM wiki_pages WHERE slug = :slug"
        ),
        {"slug": slug},
    )
    row = result.mappings().first()
    return dict(row) if row else None
