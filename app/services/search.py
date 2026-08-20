"""混合检索 — DESIGN §7

search(q) = 语义 top-k ∪ 关键词 top-k → RRF 融合
- 语义通道：embed_query(q) 加 instruct prefix → article_embeddings WHERE model=active
- 关键词通道：jieba(q) → articles.tsv @@ websearch_to_tsquery
- 融合：P1 RRF score = Σ 1/(k+rank)，k≈60
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.fts import jieba_join_async, search_wiki_fts
from app.llm.client import LLMClient

logger = logging.getLogger(__name__)

RRF_K = 60  # RRF 常数（DESIGN §7）


@dataclass
class SearchResult:
    """单条搜索结果。"""
    id: int
    title: str
    snippet: str
    score: float
    source: str  # "article" | "wiki"
    kind: str    # "semantic" | "keyword" | "hybrid"


@dataclass
class SearchResponse:
    """搜索响应。"""
    results: list[SearchResult]
    total: int
    mode: str  # "hybrid" | "semantic" | "keyword"


async def search(
    session: AsyncSession,
    query: str,
    settings: Settings,
    llm_client: LLMClient | None = None,
    mode: str = "hybrid",
    limit: int = 20,
) -> SearchResponse:
    """混合检索入口。

    Args:
        session: async session
        query: 搜索关键词
        settings: 系统配置
        llm_client: LLM 客户端（语义搜索需要）
        mode: "hybrid" | "semantic" | "keyword"
        limit: 返回结果数
    """
    if not query.strip():
        return SearchResponse(results=[], total=0, mode=mode)

    # 关键词通道
    keyword_results: list[tuple[int, float]] = []
    if mode in ("hybrid", "keyword"):
        keyword_results = await _keyword_search(session, query, limit * 2)

    # 语义通道
    semantic_results: list[tuple[int, float]] = []
    if mode in ("hybrid", "semantic"):
        if llm_client:
            try:
                semantic_results = await _semantic_search(session, query, settings, llm_client, limit * 2)
            except Exception as e:
                logger.warning("语义搜索失败，降级为关键词: %s", e)
                if mode == "hybrid":
                    mode = "keyword"
        else:
            # 无 LLM client → 降级
            if mode == "hybrid":
                mode = "keyword"
            elif mode == "semantic":
                return SearchResponse(results=[], total=0, mode="keyword")

    # RRF 融合
    if mode == "hybrid":
        merged = _rrf_merge(keyword_results, semantic_results, limit)
    elif mode == "semantic":
        merged = [(aid, score) for aid, score in semantic_results[:limit]]
    else:
        merged = [(aid, score) for aid, score in keyword_results[:limit]]

    if not merged:
        return SearchResponse(results=[], total=0, mode=mode)

    # 获取文章详情
    article_ids = [aid for aid, _ in merged]
    articles = await _get_articles(session, article_ids)
    score_map = {aid: score for aid, score in merged}

    results = []
    for aid in article_ids:
        art = articles.get(aid)
        if art:
            results.append(SearchResult(
                id=aid,
                title=art["title"] or "",
                snippet=(art["content_text"] or "")[:200],
                score=score_map.get(aid, 0.0),
                source="article",
                kind=mode,
            ))

    # 同时搜索 wiki_pages —— 按 ref_id 与已命中的 article 去重（DESIGN §7）。
    # 不能拿 wiki_pages.id 去比 article id：两者不是同一个 id 空间，
    # 撞号会误删、不撞号会漏删。
    wiki_results = await _wiki_search(session, query, limit)
    seen_article_ids = {r.id for r in results}
    for wr in wiki_results:
        if not (wr["kind"] == "article" and wr["ref_id"] in seen_article_ids):
            results.append(SearchResult(
                id=wr["id"],
                title=wr["title"] or "",
                snippet=(wr["content_md"] or "")[:200],
                score=0.0,
                source="wiki",
                kind=mode,
            ))

    return SearchResponse(results=results[:limit], total=len(results), mode=mode)


async def _keyword_search(
    session: AsyncSession,
    query: str,
    limit: int,
) -> list[tuple[int, float]]:
    """关键词通道：jieba 切词 → websearch_to_tsquery（DESIGN §5.3/§7）。"""
    q_joined = await jieba_join_async(query)
    result = await session.execute(
        text(
            "SELECT a.id, ts_rank(a.tsv, websearch_to_tsquery('simple', :q)) AS rank "
            "FROM articles a "
            "WHERE a.tsv @@ websearch_to_tsquery('simple', :q) "
            "AND a.dedupe_of IS NULL "
            "ORDER BY rank DESC "
            "LIMIT :limit"
        ),
        {"q": q_joined, "limit": limit},
    )
    return [(row[0], float(row[1])) for row in result.fetchall()]


async def _semantic_search(
    session: AsyncSession,
    query: str,
    settings: Settings,
    llm_client: LLMClient,
    limit: int,
) -> list[tuple[int, float]]:
    """语义通道：embed_query（加 instruct prefix）→ pgvector cosine 检索（DESIGN §7）。"""
    embed_resp = await llm_client.embed_query(query)
    query_vec = embed_resp.embeddings[0]
    vec_str = "[" + ",".join(str(v) for v in query_vec) + "]"
    active_model = settings.llm.embed.model

    # 语义搜索：DISTINCT ON (article_id) + ORDER BY article_id, distance
    # 替代 GROUP BY——让 pgvector planner 识别 HNSW 索引扫描路径（fix #10）。
    # TODO(partial HNSW, fix #25): 当前为兼容 DISTINCT ON 必须 ORDER BY article_id，
    #   导致非全局按 distance 最相似排序，召回质量受损。
    #   Phase 2 partial HNSW 就绪后切 ORDER BY distance + 字面量拼 active_model
    #   并 EXPLAIN 验证 HNSW 命中（DESIGN §5.2）。
    result = await session.execute(
        text(
            "SELECT DISTINCT ON (ae.article_id) "
            "ae.article_id, ae.vector <=> CAST(:vec AS vector) AS distance "
            "FROM article_embeddings ae "
            "WHERE ae.model = :model "
            "AND ae.article_id IN ("
            "  SELECT id FROM articles WHERE dedupe_of IS NULL"
            ") "
            "ORDER BY ae.article_id, distance "
            "LIMIT :limit"
        ),
        {"vec": vec_str, "model": active_model, "limit": limit},
    )
    # cosine distance 转 score：score = 1 - distance
    return [(row[0], 1.0 - float(row[1])) for row in result.fetchall()]


def _rrf_merge(
    keyword_results: list[tuple[int, float]],
    semantic_results: list[tuple[int, float]],
    limit: int,
) -> list[tuple[int, float]]:
    """RRF 融合（DESIGN §7）：score = Σ 1/(k + rank)，k=60。"""
    scores: dict[int, float] = {}

    # 关键词排名
    for rank, (aid, _) in enumerate(keyword_results, start=1):
        scores[aid] = scores.get(aid, 0.0) + 1.0 / (RRF_K + rank)

    # 语义排名
    for rank, (aid, _) in enumerate(semantic_results, start=1):
        scores[aid] = scores.get(aid, 0.0) + 1.0 / (RRF_K + rank)

    # 按 RRF 分数降序
    sorted_results = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return sorted_results[:limit]


async def _get_articles(
    session: AsyncSession,
    article_ids: list[int],
) -> dict[int, dict]:
    """批量获取文章详情。"""
    if not article_ids:
        return {}
    result = await session.execute(
        text(
            "SELECT id, title, content_text FROM articles "
            "WHERE id = ANY(:ids)"
        ),
        {"ids": article_ids},
    )
    return {row["id"]: dict(row) for row in result.mappings().all()}


async def _wiki_search(
    session: AsyncSession,
    query: str,
    limit: int,
) -> list[dict]:
    """搜索 wiki_pages 词条（DESIGN §7.1，fix #6）。

    走 tsv @@ websearch_to_tsquery('simple', jieba_join(q)) + ts_rank 排序，
    与 search_wiki 一致——这是混合检索的 wiki 通道。Phase 2 §7.1 跨表 RRF 融合
    也基于这条路径；之前 ILIKE 子串匹配的 TODO(Phase 2) 已不再适用。
    """
    wiki_ids = await search_wiki_fts(session, query, limit)
    if not wiki_ids:
        return []
    result = await session.execute(
        text(
            "SELECT id, kind, ref_id, title, content_md "
            "FROM wiki_pages WHERE id = ANY(:ids)"
        ),
        {"ids": wiki_ids},
    )
    by_id = {r["id"]: dict(r) for r in result.mappings().all()}
    return [by_id[i] for i in wiki_ids if i in by_id]
