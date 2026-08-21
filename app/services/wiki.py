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

from app.db.fts import search_wiki_fts, update_wiki_tsv

logger = logging.getLogger(__name__)


def _slugify(title: str, ref_id: int | None = None, kind: str = "article") -> str:
    """标题 → URL-friendly slug（DESIGN §6.X，fix #48）。

    Phase 2 需区分 kind：
    - article: <slug>-<article_id>（如 qwen3-...-1234）
    - topic: topic-<name-slug>-<topic_id>
    - entity: entity-<canonical_name_zh-slug>-<entity_id>
    - manual: 用户提供 slug（不自动加 id，需上层校验 UNIQUE 冲突 422）

    wiki_pages.slug 是 UNIQUE，而标题重复很常见（多家媒体转同一篇通稿、
    "本周简报" 这类固定标题、纯符号标题 slug 化后为空串）。不带 ref_id 时
    第二篇会 upsert 覆盖第一篇的正文，而 ref_id 仍指向第一篇——内容与引用错位。
    因此附加 ref_id 后缀保证一文一条。
    """
    slug = title.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = slug[:100].strip("-")
    if kind == "article":
        if ref_id is None:
            return slug
        return f"{slug}-{ref_id}" if slug else f"article-{ref_id}"
    elif kind == "topic":
        if ref_id is None:
            return f"topic-{slug}" if slug else "topic"
        return f"topic-{slug}-{ref_id}" if slug else f"topic-{ref_id}"
    elif kind == "entity":
        if ref_id is None:
            return f"entity-{slug}" if slug else "entity"
        return f"entity-{slug}-{ref_id}" if slug else f"entity-{ref_id}"
    elif kind == "manual":
        # manual slug 由用户提供，不自动加 id，需上层校验 UNIQUE 冲突 422
        if slug:
            return slug
        return f"manual-{ref_id}" if ref_id is not None else "manual"
    else:
        if ref_id is None:
            return slug
        return f"{slug}-{ref_id}" if slug else f"{ref_id}"


async def build_related_json(session: AsyncSession, article_id: int) -> list[dict]:
    """三源合并 related_json（DESIGN §6.X，fix #49）

    - same_topic: 同主题 top-5（按 score）
    - same_entity: 共现实体 top-5（按共现次数）
    - same_feed: 同源 top-3（按时间）
    合并去重取 top-10，字段 {id, title, src, score}，前端按 src 分组渲染。
    """
    related_map: dict[int, dict] = {}

    # same_topic — 同主题（score 来自 article_topics.score）
    try:
        r = await session.execute(
            text(
                "SELECT a.id, a.title, MAX(at.score) AS best_score, MAX(a.published_at) AS pub "
                "FROM article_topics at JOIN articles a ON a.id = at.article_id "
                "WHERE a.id != :aid AND a.dedupe_of IS NULL "
                "AND at.topic_id IN (SELECT topic_id FROM article_topics WHERE article_id = :aid) "
                "GROUP BY a.id, a.title ORDER BY best_score DESC, pub DESC LIMIT 5"
            ),
            {"aid": article_id},
        )
        for row in r.mappings().all():
            related_map[row["id"]] = {"id": row["id"], "title": row["title"], "src": "topic", "score": float(row["best_score"] or 0)}
    except Exception:
        pass

    # same_entity — 共现实体（通过 article_entities，若表不存在则跳过）
    try:
        r = await session.execute(
            text(
                "SELECT a.id, a.title, COUNT(*) AS cnt, MAX(a.published_at) AS pub "
                "FROM article_entities ae "
                "JOIN article_entities ae2 ON ae2.entity_id = ae.entity_id "
                "JOIN articles a ON a.id = ae2.article_id "
                "WHERE ae.article_id = :aid AND a.id != :aid AND a.dedupe_of IS NULL "
                "GROUP BY a.id, a.title ORDER BY cnt DESC, pub DESC LIMIT 5"
            ),
            {"aid": article_id},
        )
        for row in r.mappings().all():
            if row["id"] in related_map:
                continue
            # 共现次数归一化为 0.5 基准 + 计数权重
            score = 0.5 + min(float(row["cnt"]) * 0.1, 0.5)
            related_map[row["id"]] = {"id": row["id"], "title": row["title"], "src": "entity", "score": score}
    except Exception:
        pass

    # same_feed — 同源（按时间）
    try:
        r = await session.execute(
            text(
                "SELECT a.id, a.title, a.published_at AS pub FROM articles a "
                "WHERE a.feed_id = (SELECT feed_id FROM articles WHERE id = :aid) "
                "AND a.id != :aid AND a.dedupe_of IS NULL "
                "ORDER BY pub DESC LIMIT 3"
            ),
            {"aid": article_id},
        )
        for row in r.mappings().all():
            if row["id"] in related_map:
                continue
            related_map[row["id"]] = {"id": row["id"], "title": row["title"], "src": "feed", "score": 0.3}
    except Exception:
        pass

    # 合并去重后按 score 排序取 top-10
    merged = sorted(related_map.values(), key=lambda x: x["score"], reverse=True)[:10]
    return merged


async def generate_article_wiki(
    session: AsyncSession,
    article_id: int,
    settings,
) -> int | None:
    """为文章生成 wiki 词条（DESIGN §6：摘要落地后触发）。

    related_json 三源合并：同主题 + 共现实体 + 同源（§6.X）。
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

    # 三源合并 related_json（§6.X）
    related = await build_related_json(session, article_id)

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
    # 写 tsv 供全文搜索使用（fix #6）—— 必须在 commit 前，与 upsert 同一事务
    if wiki_id is not None:
        await update_wiki_tsv(session, wiki_id)
    logger.info("generate_article_wiki: article=%d → wiki=%s", article_id, wiki_id)
    return wiki_id


async def _entity_related(session: AsyncSession, entity_id: int) -> list[dict]:
    """共现该实体的文章（related_json 用，DESIGN §6.Y）。"""
    try:
        r = await session.execute(
            text(
                "SELECT a.id, a.title, a.source_url, COUNT(*) FILTER (WHERE ae.entity_id = :eid) AS cnt "
                "FROM article_entities ae "
                "JOIN articles a ON a.id = ae.article_id "
                "WHERE a.dedupe_of IS NULL "
                "AND a.id IN (SELECT article_id FROM article_entities WHERE entity_id = :eid2) "
                "GROUP BY a.id, a.title, a.source_url "
                "ORDER BY cnt DESC, a.published_at DESC LIMIT 10"
            ),
            {"eid": entity_id, "eid2": entity_id},
        )
        return [dict(x) for x in r.mappings().all()]
    except Exception:
        return []


async def generate_entity_wiki(
    session: AsyncSession,
    entity_id: int,
    settings,
) -> int | None:
    """为实体生成 wiki 词条（DESIGN §6.Y，kind='entity'，slug=entity-<name>-<id>）。

    fix #80：此前该函数不存在，handler 忽略 entity_ids 循环调 generate_article_wiki，
    重复生成文章词条。现按实体描述 + 共现文章生成独立实体页。
    返回 wiki_page id。
    """
    result = await session.execute(
        text("SELECT canonical_name_zh, description FROM entities WHERE id=:eid"),
        {"eid": entity_id},
    )
    row = result.mappings().first()
    if not row or not row["canonical_name_zh"]:
        return None

    title = row["canonical_name_zh"]
    desc = row["description"] or ""
    related = await _entity_related(session, entity_id)

    md_parts = [f"# {title}\n"]
    if desc:
        md_parts.append(f"{desc}\n")
    if related:
        md_parts.append("## 相关文章\n")
        for r in related:
            href = r.get("source_url") or "#"
            md_parts.append(f"- [{r.get('title') or r['id']}]({href})")
        md_parts.append("")
    content_md = "\n".join(md_parts)

    slug = _slugify(title, entity_id, "entity")
    related_json = [
        {"id": r["id"], "title": r.get("title"), "src": "entity", "score": 0.0}
        for r in related
    ]
    await session.execute(
        text(
            "INSERT INTO wiki_pages (kind, ref_id, title, slug, content_md, related_json) "
            "VALUES ('entity', :ref_id, :title, :slug, :content, :related) "
            "ON CONFLICT (slug) DO UPDATE "
            "SET ref_id = EXCLUDED.ref_id, title = EXCLUDED.title, "
            "    content_md = EXCLUDED.content_md, "
            "    related_json = EXCLUDED.related_json, updated_at = now()"
        ),
        {
            "ref_id": entity_id,
            "title": title,
            "slug": slug,
            "content": content_md,
            "related": json.dumps(related_json, ensure_ascii=False),
        },
    )
    r2 = await session.execute(text("SELECT id FROM wiki_pages WHERE slug=:slug"), {"slug": slug})
    row2 = r2.first()
    wiki_id = row2[0] if row2 else None
    if wiki_id is not None:
        await update_wiki_tsv(session, wiki_id)
    logger.info("generate_entity_wiki: entity=%d → wiki=%s", entity_id, wiki_id)
    return wiki_id


async def generate_topic_wiki(
    session: AsyncSession,
    topic_id: int,
    settings,
) -> int | None:
    """为主题生成 wiki 词条（DESIGN §6.Y，kind='topic'，slug=topic-<name>-<id>）。

    fix #80：此前该函数不存在，handler 忽略 topic_ids 循环调 generate_article_wiki。
    返回 wiki_page id。
    """
    result = await session.execute(
        text("SELECT name, description FROM topics WHERE id=:tid"),
        {"tid": topic_id},
    )
    row = result.mappings().first()
    if not row or not row["name"]:
        return None

    title = row["name"]
    desc = row["description"] or ""

    # 主题下文章（经 article_topics）
    related = []
    try:
        r = await session.execute(
            text(
                "SELECT a.id, a.title, a.source_url FROM article_topics at "
                "JOIN articles a ON a.id = at.article_id "
                "WHERE at.topic_id = :tid AND a.dedupe_of IS NULL "
                "ORDER BY at.score DESC, a.published_at DESC LIMIT 10"
            ),
            {"tid": topic_id},
        )
        related = [dict(x) for x in r.mappings().all()]
    except Exception:
        pass

    md_parts = [f"# {title}\n"]
    if desc:
        md_parts.append(f"{desc}\n")
    if related:
        md_parts.append("## 相关文章\n")
        for a in related:
            href = a.get("source_url") or "#"
            md_parts.append(f"- [{a.get('title') or a['id']}]({href})")
        md_parts.append("")
    content_md = "\n".join(md_parts)

    slug = _slugify(title, topic_id, "topic")
    related_json = [
        {"id": a["id"], "title": a.get("title"), "src": "topic", "score": 0.0}
        for a in related
    ]
    await session.execute(
        text(
            "INSERT INTO wiki_pages (kind, ref_id, title, slug, content_md, related_json) "
            "VALUES ('topic', :ref_id, :title, :slug, :content, :related) "
            "ON CONFLICT (slug) DO UPDATE "
            "SET ref_id = EXCLUDED.ref_id, title = EXCLUDED.title, "
            "    content_md = EXCLUDED.content_md, "
            "    related_json = EXCLUDED.related_json, updated_at = now()"
        ),
        {
            "ref_id": topic_id,
            "title": title,
            "slug": slug,
            "content": content_md,
            "related": json.dumps(related_json, ensure_ascii=False),
        },
    )
    r2 = await session.execute(text("SELECT id FROM wiki_pages WHERE slug=:slug"), {"slug": slug})
    row2 = r2.first()
    wiki_id = row2[0] if row2 else None
    if wiki_id is not None:
        await update_wiki_tsv(session, wiki_id)
    logger.info("generate_topic_wiki: topic=%d → wiki=%s", topic_id, wiki_id)
    return wiki_id


async def search_wiki(
    session: AsyncSession,
    query: str,
    limit: int = 20,
) -> list[dict]:
    """搜索 wiki 词条（关键词全文搜索，PRD §15 验收 5，fix #6）。

    走 tsv @@ websearch_to_tsquery('simple', ...) + ts_rank 排序，
    与 search_articles_fts 同一模式——jieba 预切词保证中文多词召回。
    """
    wiki_ids = await search_wiki_fts(session, query, limit)
    if not wiki_ids:
        return []
    # 按 ts_rank 顺序批量回填 title/content_md
    result = await session.execute(
        text(
            "SELECT id, kind, ref_id, title, slug, content_md "
            "FROM wiki_pages WHERE id = ANY(:ids)"
        ),
        {"ids": wiki_ids},
    )
    by_id = {r["id"]: dict(r) for r in result.mappings().all()}
    return [by_id[i] for i in wiki_ids if i in by_id]


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
