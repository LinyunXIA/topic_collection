"""知识图谱 — Phase 2 切片 2.4（DESIGN §6.X）

- graph_json(topic_id, entity_type, since_days) -> {categories, nodes, links, filters}
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def graph_json(
    session: AsyncSession,
    *,
    topic_id: int | None = None,
    entity_type: str | None = None,
    since_days: int | None = None,
    max_nodes: int = 300,
) -> dict:
    """ECharts force-graph JSON"""
    # 类别
    categories = [{"name": t} for t in ["person", "org", "model", "technology", "product", "concept", "other"]]

    # 节点：entities
    where = []
    params: dict = {"limit": max_nodes}
    if entity_type:
        where.append("e.entity_type = :etype")
        params["etype"] = entity_type
    if since_days:
        where.append("e.first_seen_at > now() - INTERVAL '1 day' * :days")
        params["days"] = since_days
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    result = await session.execute(
        text(f"SELECT id, canonical_name_zh, entity_type, mention_count FROM entities e {where_sql} ORDER BY mention_count DESC LIMIT :limit"),
        params,
    )
    nodes = []
    for row in result.mappings().all():
        nodes.append(
            {
                "id": str(row["id"]),
                "name": row["canonical_name_zh"],
                "category": row["entity_type"] or "other",
                "value": row["mention_count"] or 1,
                "symbolSize": min(30, 10 + (row["mention_count"] or 1) * 2),
            }
        )

    # 边：relations
    result = await session.execute(
        text(
            "SELECT subject_id, object_id, predicate, confidence FROM relations ORDER BY confidence DESC LIMIT :limit"
        ),
        {"limit": max_nodes},
    )
    links = []
    node_ids = {n["id"] for n in nodes}
    for row in result.mappings().all():
        sid = str(row["subject_id"])
        oid = str(row["object_id"])
        if sid in node_ids and oid in node_ids:
            links.append({"source": sid, "target": oid, "label": row["predicate"], "value": float(row["confidence"] or 0.5)})

    return {"categories": categories, "nodes": nodes, "links": links, "filters": {"topic_id": topic_id, "entity_type": entity_type, "since_days": since_days}}


async def graph_node_articles(session: AsyncSession, node_id: int, limit: int = 10) -> list[dict]:
    """节点回看文章"""
    result = await session.execute(
        text(
            "SELECT a.id, a.title FROM articles a JOIN article_entities ae ON ae.article_id=a.id WHERE ae.entity_id=:eid AND a.dedupe_of IS NULL ORDER BY a.fetched_at DESC LIMIT :limit"
        ),
        {"eid": node_id, "limit": limit},
    )
    return [dict(row) for row in result.mappings().all()]
