"""实体抽取与归并 — Phase 2 切片 2.3（DESIGN §6.Y）

- extract_entities(article_id) / upsert / merge_aliases / resolve
"""

from __future__ import annotations

import json
import logging
from difflib import SequenceMatcher

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.client import PermanentError

logger = logging.getLogger(__name__)


async def normalize_surface(content_text: str, surface: str) -> str | None:
    """在原文中找 surface 的最近邻 span（grounding 修正，DESIGN §4.6.1）"""
    if not surface or not content_text:
        return None
    if surface in content_text:
        return surface
    # 简单模糊：找最相似的子串（长度与 surface 相当）
    best = None
    best_ratio = 0.0
    n = len(surface)
    # 滑动窗口，步长 1，找最相似
    for i in range(max(0, len(content_text) - n + 1)):
        window = content_text[i : i + n]
        ratio = SequenceMatcher(None, surface, window).ratio()
        if ratio > best_ratio and ratio > 0.8:
            best_ratio = ratio
            best = window
    return best


async def _build_entity_id_map(session: AsyncSession, parsed: dict) -> dict[tuple[str, str], int]:
    """返回 {(entity_type, canonical_name_zh): id}"""
    keys = [(e.get("type", "other"), e["canonical_name_zh"]) for e in parsed.get("entities", []) if e.get("canonical_name_zh")]
    if not keys:
        return {}
    # 用 ANY 需传 array of composite type，但 asyncpg 对 tuple 支持有限，改用 OR 展开（小批量可接受）
    # 简化：逐个查
    result = {}
    for typ, zh in keys:
        r = await session.execute(
            text("SELECT id FROM entities WHERE entity_type=:typ AND canonical_name_zh=:zh"),
            {"typ": typ, "zh": zh},
        )
        row = r.first()
        if row:
            result[(typ, zh)] = row[0]
    return result


async def _detect_new_or_changed_entities(session: AsyncSession, article_id: int, entity_ids: list[int]) -> list[int]:
    """简化：无 wiki 即需生成"""
    if not entity_ids:
        return []
    # 查哪些 entity 还没有 wiki
    result = await session.execute(
        text("SELECT ref_id FROM wiki_pages WHERE kind='entity' AND ref_id = ANY(:ids)"),
        {"ids": entity_ids},
    )
    existing = {r[0] for r in result.fetchall()}
    return [eid for eid in entity_ids if eid not in existing]


async def complete_extract(
    session: AsyncSession,
    article_id: int,
    content_hash: str,
    parsed: dict,
    *,
    content_text: str,
    settings=None,
) -> None:
    """公共钩子（DESIGN §6.Y）：entities/article_entities/relations + wiki 入队 + done"""
    from app.pipeline import check_and_set_done

    # 0. content_hash 必须是 sha256 64 位十六进制（版本守卫可比，DESIGN §6；fix #81
    #    曾把正文前 100 字符误当 content_hash 传入 → downstream 版本守卫恒不通过）
    import re as _re

    if not (_re.fullmatch(r"[0-9a-f]{64}", content_hash) if isinstance(content_hash, str) else False):
        raise PermanentError(
            f"complete_extract: content_hash 非法（期望 sha256 64 位十六进制，实际前 32 字符='{str(content_hash)[:32]}'）"
        )

    # 1. entities upsert
    for ent in parsed.get("entities", []):
        surface = ent.get("surface")
        if surface and content_text and surface not in content_text:
            aligned = await normalize_surface(content_text, surface)
            if aligned:
                ent["surface"] = aligned
            else:
                ent["confidence"] = (ent.get("confidence") or 0.5) * 0.5
                if ent["confidence"] is not None and ent["confidence"] < 0.1:
                    continue
        await session.execute(
            text(
                """
                INSERT INTO entities (canonical_name_zh, aliases_json, entity_type, description, mention_count, confidence)
                VALUES (:zh, :aliases, :type, :desc, 0, :conf)
                ON CONFLICT (entity_type, canonical_name_zh) DO UPDATE SET
                  aliases_json = (
                    SELECT jsonb_agg(DISTINCT v)
                    FROM jsonb_array_elements(
                      COALESCE(entities.aliases_json, '[]'::jsonb) ||
                      COALESCE(EXCLUDED.aliases_json, '[]'::jsonb)
                    ) v
                  ),
                  description = CASE WHEN EXCLUDED.confidence > entities.confidence
                                     THEN EXCLUDED.description ELSE entities.description END,
                  mention_count = (SELECT COUNT(*) FROM article_entities ae WHERE ae.entity_id = entities.id),
                  confidence = GREATEST(entities.confidence, EXCLUDED.confidence),
                  last_seen_at = now()
                """
            ),
            {
                "zh": ent.get("canonical_name_zh"),
                "aliases": json.dumps(ent.get("aliases", []), ensure_ascii=False),
                "type": ent.get("type", "other"),
                "desc": ent.get("description"),
                "conf": ent.get("confidence", 0.5),
            },
        )

    eid_map = await _build_entity_id_map(session, parsed)

    # 3. article_entities
    for ent in parsed.get("entities", []):
        zh = ent.get("canonical_name_zh")
        typ = ent.get("type", "other")
        eid = eid_map.get((typ, zh))
        if not eid:
            continue
        await session.execute(
            text(
                """
                INSERT INTO article_entities (article_id, entity_id, confidence, surface)
                VALUES (:a, :e, :c, :s)
                ON CONFLICT (article_id, entity_id) DO UPDATE SET
                  confidence = GREATEST(article_entities.confidence, EXCLUDED.confidence),
                  surface = EXCLUDED.surface
                """
            ),
            {"a": article_id, "e": eid, "c": ent.get("confidence", 0.5), "s": ent.get("surface")},
        )

    # 4. relations
    name_to_eid = {zh: eid for (typ, zh), eid in eid_map.items()}
    for rel in parsed.get("relations", []):
        sid = name_to_eid.get(rel.get("subject"))
        oid = name_to_eid.get(rel.get("object"))
        if not sid or not oid:
            continue
        await session.execute(
            text(
                """
                INSERT INTO relations (subject_id, predicate, object_id, source_articles_json, confidence, last_seen_at)
                VALUES (:s, :p, :o, jsonb_build_array(:aid::bigint), :c, now())
                ON CONFLICT (subject_id, predicate, object_id) DO UPDATE SET
                  source_articles_json = (
                    SELECT jsonb_agg(DISTINCT v)
                    FROM jsonb_array_elements(
                      COALESCE(relations.source_articles_json, '[]'::jsonb) ||
                      COALESCE(EXCLUDED.source_articles_json, '[]'::jsonb)
                    ) v
                  ),
                  confidence = GREATEST(relations.confidence, EXCLUDED.confidence),
                  last_seen_at = now()
                """
            ),
            {"s": sid, "p": rel.get("predicate"), "o": oid, "aid": article_id, "c": rel.get("confidence", 0.5)},
        )

    # 5. wiki 入队（fix #80：把 new_ids 经 enqueue_entity_wiki 真正写入 payload_json，
    #    #46 的 payload 合并策略才有意义；否则 payload 恒 NULL、handler 无 entity_ids 可遍历）
    new_ids = await _detect_new_or_changed_entities(session, article_id, list(eid_map.values()))
    if new_ids:
        from app.pipeline import enqueue_entity_wiki

        await enqueue_entity_wiki(session, article_id, new_ids, content_hash)

    await check_and_set_done(session, article_id)


async def extract_entities(session: AsyncSession, article_id: int, settings, llm_client) -> None:
    """Phase 2 切片 2.3 入口：读文章 → 调 LLM → complete_extract"""
    from app.llm.base import GenerateRequest
    from app.llm.prompts import get_prompt
    from app.llm.structured import parse_with_repair

    result = await session.execute(
        text("SELECT title, content_text, lang, content_hash FROM articles WHERE id=:aid"),
        {"aid": article_id},
    )
    row = result.mappings().first()
    if not row or not row["content_text"]:
        raise PermanentError(f"article {article_id} 内容为空")
    system, user = get_prompt("extract_entities", title=row["title"], content=row["content_text"][:8000], lang=row["lang"])
    model = settings.llm.generate.model if settings.llm.generate else settings.llm.model
    # per-task 覆盖
    model = settings.llm.models.get("extract_entities", model)
    resp = await llm_client.generate(GenerateRequest(model=model, messages=[{"role": "system", "content": system}, {"role": "user", "content": user}], json_mode=True))
    parsed = parse_with_repair(resp.text, expected_keys=["entities", "relations"])
    if not parsed:
        raise PermanentError(f"JSON 解析失败: {resp.text[:200]}")
    # fix #81：传真实 content_hash（版本守卫可比），勿传正文片段
    await complete_extract(session, article_id, row["content_hash"], parsed, content_text=row["content_text"], settings=settings)


async def merge_aliases(session: AsyncSession, alias: str, entity_type: str, canonical_zh: str):
    """把 alias 折叠到 canonical_zh（pg_trgm 相似度 >0.6）"""
    # 1. 找候选
    candidates = await session.execute(
        text(
            """
            SELECT id, canonical_name_zh FROM entities
            WHERE entity_type=:type AND (canonical_name_zh % :alias OR :alias = ANY(SELECT jsonb_array_elements_text(aliases_json)))
            """
        ),
        {"type": entity_type, "alias": alias},
    )
    # 简化：不实际合并，仅示例
    return [dict(r) for r in candidates.mappings().all()]


async def resolve_entity(session: AsyncSession, name: str):
    """按别名或主名解析实体"""
    result = await session.execute(
        text("SELECT id, canonical_name_zh, entity_type FROM entities WHERE canonical_name_zh=:name OR :name = ANY(SELECT jsonb_array_elements_text(aliases_json))"),
        {"name": name},
    )
    return result.mappings().first()
