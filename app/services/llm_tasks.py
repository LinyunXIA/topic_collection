"""LLM 任务处理 — summarize / embed / topics 入队钩子（DESIGN §6）

complete_summarize(): summaries upsert + tsv 刷新 + embed_summary/topics/wiki 入队
complete_embed(): embeddings upsert + 近似去重判定 + done 检查
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.fts import update_article_tsv
from app.llm.base import GenerateRequest
from app.llm.client import LLMClient, PermanentError
from app.llm.prompts import get_prompt
from app.llm.structured import parse_with_repair
from app.pipeline import check_and_set_done, enqueue_jobs

logger = logging.getLogger(__name__)


# ── Summarize 任务 ─────────────────────────────────────────────────

async def run_summarize(
    session: AsyncSession,
    job: dict,
    settings: Settings,
    llm_client: LLMClient | None,
) -> None:
    """处理 summarize 任务：调用 LLM → complete_summarize 钩子。"""
    article_id = job["article_id"]
    content_hash = job["content_hash"]

    # 获取文章内容
    result = await session.execute(
        text("SELECT title, content_text, lang FROM articles WHERE id=:aid"),
        {"aid": article_id},
    )
    row = result.mappings().first()
    if not row:
        raise PermanentError(f"文章 {article_id} 不存在")

    title = row["title"]
    content = row["content_text"] or ""

    if not content.strip():
        raise PermanentError(f"文章 {article_id} 内容为空")

    # 调用 LLM
    if not llm_client:
        raise PermanentError("LLM client 未初始化")

    system, user = get_prompt("summarize", title=title, content=content[:8000])
    # 注意：fallback 用 generate.model 而非顶层 llm.model
    # 顶层 llm.model 默认是本地 oMLX 模型（如 Qwen3.8），切到外部 API 会 400
    _gen = settings.llm.generate
    _default = _gen.model if _gen else settings.llm.model
    model = settings.llm.models.get("summarize", _default)
    resp = await llm_client.generate(
        GenerateRequest(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            json_mode=True,
        )
    )

    # 解析结果
    parsed = parse_with_repair(resp.text, expected_keys=["summary_zh", "key_points", "confidence"])
    if not parsed:
        raise PermanentError(f"JSON 解析失败: {resp.text[:200]}")

    await complete_summarize(session, article_id, content_hash, parsed, settings)


async def complete_summarize(
    session: AsyncSession,
    article_id: int,
    content_hash: str,
    result: dict,
    settings: Settings,
) -> None:
    """complete_summarize() 公共钩子（DESIGN §6）。

    职责（同事务）：
    1. summaries upsert（content_hash 版本守卫）
    2. tsv 刷新（关键词通道补全）
    3. embed_summary 入队
    4. topics 入队（仅关键词未命中）
    5. wiki 入队
    6. done 检查
    """
    summary_text = result.get("summary_zh", "")
    key_points = result.get("key_points", [])
    confidence = result.get("confidence", 0.0)
    # 注意：fallback 用 generate.model 而非顶层 llm.model（DESIGN §4.1 / §9 per-capability）
    _gen = settings.llm.generate
    _default = _gen.model if _gen else settings.llm.model
    model = settings.llm.models.get("summarize", _default)

    # 1. summaries upsert（content_hash 版本守卫）
    await session.execute(
        text(
            "INSERT INTO summaries (article_id, lang, model, content_hash, "
            "  summary_text, key_points_json, confidence) "
            "VALUES (:aid, 'zh', :model, :ch, :summary, :kp, :conf) "
            "ON CONFLICT (article_id, lang, model) DO UPDATE "
            "SET content_hash=EXCLUDED.content_hash, "
            "    summary_text=EXCLUDED.summary_text, "
            "    key_points_json=EXCLUDED.key_points_json, "
            "    confidence=EXCLUDED.confidence "
            "WHERE EXCLUDED.content_hash = "
            "  (SELECT content_hash FROM articles WHERE id = EXCLUDED.article_id)"
        ),
        {
            "aid": article_id,
            "model": model,
            "ch": content_hash,
            "summary": summary_text,
            "kp": json.dumps(key_points, ensure_ascii=False),
            "conf": confidence,
        },
    )

    # 2. tsv 刷新（关键词通道补全）
    key_points_text = " ".join(key_points) if key_points else ""
    # title/content_text 留空 = 由 update_article_tsv 从 articles 读回原文段一起重建。
    # tsv 是整列覆盖写，只传 summary 段会把入库时建好的原文索引抹掉（DESIGN §5.3）。
    await update_article_tsv(
        session, article_id,
        title="", content_text="",
        summary_text=summary_text,
        key_points_text=key_points_text,
    )

    # 3. embed_summary 入队
    await enqueue_jobs(session, article_id, ["embed_summary"], content_hash)

    # 4. topics 入队（仅关键词未命中 — 由 topics.py 判断）
    # 这里简单入队，match_keywords 在 ingest 时已判断
    # complete_summarize 时检查是否有 keyword 命中
    result_check = await session.execute(
        text(
            "SELECT COUNT(*) FROM article_topics "
            "WHERE article_id=:aid AND method='keyword'"
        ),
        {"aid": article_id},
    )
    has_keyword = result_check.scalar() > 0
    if not has_keyword:
        await enqueue_jobs(session, article_id, ["topics"], content_hash)

    # 5. wiki 入队
    await enqueue_jobs(session, article_id, ["wiki"], content_hash)

    # 6. 翻译入队（仅非中文，DESIGN §6.Z）
    # 仅当 lang != 'zh' 时入队 translate，后台慢任务，不阻塞 done（check_and_set_done 排除 translate）
    try:
        lang_row = await session.execute(text("SELECT lang FROM articles WHERE id=:aid"), {"aid": article_id})
        lang_val = lang_row.scalar()
        if lang_val and lang_val != "zh":
            await enqueue_jobs(session, article_id, ["translate"], content_hash)
    except Exception:
        pass  # 翻译入队失败不影响主流程

    # 7. done 检查
    await check_and_set_done(session, article_id)

    logger.info("complete_summarize: article=%d 完成", article_id)


# ── 翻译任务（Phase 2 切片 2.2，§6.Z） ─────────────────────────────

async def run_translate(
    session: AsyncSession,
    job: dict,
    settings: Settings,
    llm_client: LLMClient | None,
) -> None:
    """处理 translate 任务：全文译为中文（DESIGN §6.Z，本地 27B 后台慢任务）。"""
    article_id = job["article_id"]
    content_hash = job["content_hash"]
    result = await session.execute(
        text("SELECT title, content_text, lang FROM articles WHERE id=:aid"),
        {"aid": article_id},
    )
    row = result.mappings().first()
    if not row:
        raise PermanentError(f"文章 {article_id} 不存在")
    if not row["content_text"] or not row["content_text"].strip():
        raise PermanentError(f"文章 {article_id} 内容为空")
    if not llm_client:
        raise PermanentError("LLM client 未初始化")
    # 中文文章无需翻译，直接完成
    if row["lang"] == "zh":
        await complete_translate(session, article_id, content_hash, "", "", settings, job)
        return
    system, user = get_prompt("translate", title=row["title"], content=row["content_text"][:8000])
    _gen = settings.llm.generate
    _default = _gen.model if _gen else settings.llm.model
    model = settings.llm.models.get("translate", _default)
    resp = await llm_client.generate(
        GenerateRequest(model=model, messages=[{"role": "system", "content": system}, {"role": "user", "content": user}], json_mode=False)
    )
    text_out = resp.text.strip() if resp.text else ""
    if not text_out:
        raise PermanentError("翻译结果为空")
    # 标题翻译：简单用原文标题，前 100 字符作为译后标题（Phase 2 可单调 translate_title）
    await complete_translate(session, article_id, content_hash, row["title"], text_out, settings, job)


async def complete_translate(
    session: AsyncSession,
    article_id: int,
    content_hash: str,
    translated_title: str,
    translated_content: str,
    settings: Settings,
    job: dict | None = None,
) -> None:
    """complete_translate 钩子（DESIGN §6.Z）"""
    # 中文原文无需翻译时，translated_content 为空，直接 done
    if not translated_content:
        if job is not None:
            await session.execute(
                text("UPDATE processing_jobs SET status='succeeded', lock_until=NULL, updated_at=now() WHERE id=:jid AND status='running'"),
                {"jid": job["id"]},
            )
        await check_and_set_done(session, article_id)
        return
    # 取 lang
    result = await session.execute(text("SELECT lang FROM articles WHERE id=:aid"), {"aid": article_id})
    src_lang = result.scalar() or "en"
    _gen = settings.llm.generate
    _default = _gen.model if _gen else settings.llm.model
    model = settings.llm.models.get("translate", _default)
    await session.execute(
        text(
            "INSERT INTO translations (article_id, src_lang, tgt_lang, model, content_hash, translated_title, translated_content) "
            "VALUES (:aid, :src, 'zh', :model, :ch, :tt, :tc) "
            "ON CONFLICT (article_id, src_lang, tgt_lang, model) DO UPDATE "
            "SET content_hash=EXCLUDED.content_hash, translated_title=EXCLUDED.translated_title, translated_content=EXCLUDED.translated_content "
            "WHERE EXCLUDED.content_hash = (SELECT content_hash FROM articles WHERE id=EXCLUDED.article_id)"
        ),
        {"aid": article_id, "src": src_lang, "model": model, "ch": content_hash, "tt": translated_title[:500], "tc": translated_content},
    )
    if job is not None:
        await session.execute(
            text("UPDATE processing_jobs SET status='succeeded', lock_until=NULL, updated_at=now() WHERE id=:jid AND status='running'"),
            {"jid": job["id"]},
        )
    await check_and_set_done(session, article_id)
    logger.info("complete_translate: article=%d 完成", article_id)


# ── Embed 任务 ─────────────────────────────────────────────────────

async def run_embed_core(
    session: AsyncSession,
    job: dict,
    settings: Settings,
    llm_client: LLMClient | None,
) -> None:
    """处理 embed_core 任务：title + body 两条向量。"""
    article_id = job["article_id"]
    content_hash = job["content_hash"]

    result = await session.execute(
        text("SELECT title, content_text FROM articles WHERE id=:aid"),
        {"aid": article_id},
    )
    row = result.mappings().first()
    if not row:
        raise PermanentError(f"文章 {article_id} 不存在")

    if not llm_client:
        raise PermanentError("LLM client 未初始化")

    title = row["title"]
    body = (row["content_text"] or "")[:8192]  # §5.2: body 截断 8K

    # embed title（不加 instruct prefix）
    title_resp = await llm_client.embed([title])
    # embed body（不加 instruct prefix）
    body_resp = await llm_client.embed([body])

    await complete_embed(session, article_id, content_hash, [
        ("title", title_resp.embeddings[0], title_resp.dim),
        ("body", body_resp.embeddings[0], body_resp.dim),
    ], settings, job=job)


async def run_embed_summary(
    session: AsyncSession,
    job: dict,
    settings: Settings,
    llm_client: LLMClient | None,
) -> None:
    """处理 embed_summary 任务：summary 一条向量。"""
    article_id = job["article_id"]
    content_hash = job["content_hash"]

    result = await session.execute(
        text(
            "SELECT summary_text FROM summaries "
            "WHERE article_id=:aid AND lang='zh' "
            "ORDER BY (model = :model) DESC, id DESC LIMIT 1"
        ),
        {"aid": article_id, "model": settings.llm.models.get(
            "summarize",
            settings.llm.generate.model if settings.llm.generate else settings.llm.model,
        )},
    )
    row = result.first()
    if not row or not row[0]:
        raise PermanentError(f"文章 {article_id} 的 summary 不存在")

    if not llm_client:
        raise PermanentError("LLM client 未初始化")

    resp = await llm_client.embed([row[0]])
    await complete_embed(session, article_id, content_hash, [
        ("summary", resp.embeddings[0], resp.dim),
    ], settings, job=job)


async def complete_embed(
    session: AsyncSession,
    article_id: int,
    content_hash: str,
    embeddings: list[tuple[str, list[float], int]],
    settings: Settings,
    job: dict | None = None,
) -> None:
    """complete_embed() 公共钩子（DESIGN §6）。

    职责（同事务）：
    1. embeddings upsert（content_hash 版本守卫）
    2. embed_core 完成后做近似去重判定（仅 kind='body'）
    3. job 状态推进
    4. done 检查
    """
    model = settings.llm.embed.model

    for kind, vector, dim in embeddings:
        # 维度校验
        if dim != settings.db.vector_dim:
            raise PermanentError(
                f"向量维度不匹配: 实测={dim}, 配置={settings.db.vector_dim}"
            )

        # 1. embeddings upsert（content_hash 版本守卫）
        # 注意：:vec::vector 的 :: 会被 asyncpg 误解析，改用 format=true 内联向量
        vec_str = "[" + ",".join(str(v) for v in vector) + "]"
        await session.execute(
            text(
                "INSERT INTO article_embeddings "
                "(article_id, kind, model, content_hash, dim, vector) "
                "VALUES (:aid, :kind, :model, :ch, :dim, CAST(:vec AS vector)) "
                "ON CONFLICT (article_id, kind, model) DO UPDATE "
                "SET content_hash=EXCLUDED.content_hash, "
                "    dim=EXCLUDED.dim, "
                "    vector=EXCLUDED.vector "
                "WHERE EXCLUDED.content_hash = "
                "  (SELECT content_hash FROM articles WHERE id = EXCLUDED.article_id)"
            ),
            {
                "aid": article_id,
                "kind": kind,
                "model": model,
                "ch": content_hash,
                "dim": dim,
                "vec": vec_str,
            },
        )

    # 2. 近似去重判定（仅 embed_core 的 body 完成后）
    is_body_embed = any(k == "body" for k, _, _ in embeddings)
    is_core = job is not None and job.get("task") == "embed_core"
    if is_body_embed and is_core:
        await _check_near_dedup(session, article_id, settings)

    # 3. job 状态推进
    if job is not None:
        await session.execute(
            text(
                "UPDATE processing_jobs SET status='succeeded', lock_until=NULL, updated_at=now() "
                "WHERE id=:jid AND status='running'"
            ),
            {"jid": job["id"]},
        )

    # 4. done 检查
    await check_and_set_done(session, article_id)

    task_name = job.get("task", "unknown") if job else "unknown"
    logger.info("complete_embed: article=%d task=%s 完成", article_id, task_name)


async def _check_near_dedup(
    session: AsyncSession,
    article_id: int,
    settings: Settings,
) -> None:
    """近似去重判定（DESIGN §6）—— body↔body 同粒度。"""
    # 获取本文章的 body 向量和 lang
    result = await session.execute(
        text(
            "SELECT ae.vector, a.lang "
            "FROM article_embeddings ae "
            "JOIN articles a ON a.id = ae.article_id "
            "WHERE ae.article_id=:aid AND ae.kind='body' AND ae.model=:model"
        ),
        {"aid": article_id, "model": settings.llm.embed.model},
    )
    row = result.mappings().first()
    if not row or not row["vector"]:
        return

    body_vec = row["vector"]
    article_lang = row["lang"]

    # 检索候选
    threshold = settings.ingestion.dedup.threshold
    window_days = settings.ingestion.dedup.window_days
    k = settings.ingestion.dedup.k
    same_lang = settings.ingestion.dedup.same_lang_only

    lang_filter = "AND a.lang = :lang" if same_lang else ""
    result = await session.execute(
        text(
            f"SELECT ae.article_id, ae.vector <=> CAST(:vec AS vector) AS distance "
            f"FROM article_embeddings ae "
            f"JOIN articles a ON a.id = ae.article_id "
            f"WHERE ae.kind='body' AND ae.model=:model "
            f"AND ae.article_id != :aid "
            f"AND a.dedupe_of IS NULL "
            f"AND a.fetched_at > now() - INTERVAL '{window_days} days' "
            f"{lang_filter} "
            f"ORDER BY ae.vector <=> CAST(:vec AS vector) "
            f"LIMIT :k"
        ),
        {
            "vec": str(body_vec),
            "model": settings.llm.embed.model,
            "aid": article_id,
            "k": k,
            **({"lang": article_lang} if same_lang else {}),
        },
    )
    candidates = result.fetchall()

    for cand_id, distance in candidates:
        if distance <= (1 - threshold):
            logger.info(
                "近似去重命中: article=%d → winner=%d (distance=%.4f)",
                article_id, cand_id, distance,
            )
            # 沿 dedupe_of 链回溯到终极 winner
            winner_id = await _find_ultimate_winner(session, cand_id)

            # loser status='done' + dedupe_of
            loser_res = await session.execute(
                text(
                    "UPDATE articles SET status='done', dedupe_of=:winner "
                    "WHERE id=:aid AND status='processing'"
                ),
                {"aid": article_id, "winner": winner_id},
            )
            merged = loser_res.rowcount > 0

            # mention_count 累计转移到 winner —— 仅在 loser 本次确实被标记时执行，
            # 否则重跑同一 job 会重复累加
            if merged:
                await session.execute(
                    text(
                        "UPDATE articles SET mention_count = mention_count + "
                        "  (SELECT mention_count FROM articles WHERE id=:aid) "
                        "WHERE id=:winner"
                    ),
                    {"aid": article_id, "winner": winner_id},
                )

            # supersede summarize/topics/wiki
            await session.execute(
                text(
                    "UPDATE processing_jobs SET status='superseded', updated_at=now() "
                    "WHERE article_id=:aid "
                    "AND task IN ('summarize','topics','wiki') "
                    "AND status IN ('queued','running')"
                ),
                {"aid": article_id},
            )

            # 双保险删 article_topics
            await session.execute(
                text("DELETE FROM article_topics WHERE article_id=:aid"),
                {"aid": article_id},
            )

            # 审计：fetch_events.feed_id 是 NOT NULL + FK→feeds.id，
            # 这里没有 feed 上下文，写 0 会外键违例并回滚整个 embed 事务。
            # TODO(Phase 2): 建独立的 dedup_events 审计表后改回落库。
            logger.info(
                "dedup_merge: loser=%d winner=%d distance=%.4f",
                article_id, winner_id, distance,
            )

            break  # 只合并第一个命中


async def _find_ultimate_winner(session: AsyncSession, article_id: int) -> int:
    """沿 dedupe_of 链回溯到终极 winner（DESIGN §6 多跳扁平化）。"""
    result = await session.execute(
        text(
            "WITH RECURSIVE chain AS ("
            "  SELECT id, dedupe_of FROM articles WHERE id=:aid "
            "  UNION ALL "
            "  SELECT a.id, a.dedupe_of FROM articles a "
            "  JOIN chain c ON a.id = c.dedupe_of "
            ") SELECT id FROM chain WHERE dedupe_of IS NULL LIMIT 1"
        ),
        {"aid": article_id},
    )
    row = result.first()
    return row[0] if row else article_id
