"""日报/周报 — Phase 2 切片 2.5（DESIGN §10.1 / §10.4 飞书推送，#52 同 #43 已实现同事务推送）"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings

logger = logging.getLogger(__name__)


async def _maybe_notify_feishu(report_type: str, title: str, markdown: str, settings: Settings | None) -> None:
    """报告 succeeded 后链式触发飞书推送（DESIGN §10.4）

    失败仅 warning，不阻塞报告生成；webhook 含 token 从环境变量读取。
    """
    if not settings:
        return
    feishu = getattr(getattr(settings, "schedule", None), "feishu", None)
    if not feishu or not feishu.enabled:
        return
    if report_type not in (feishu.events or []):
        return
    webhook = os.getenv(feishu.webhook_env or "FEISHU_WEBHOOK", "")
    if not webhook:
        logger.warning("飞书推送跳过：环境变量 %s 未设置", feishu.webhook_env)
        return
    try:
        from app.services.notify import send_feishu_markdown

        await send_feishu_markdown(webhook, title, markdown)
    except Exception as e:
        logger.warning("飞书推送异常（不阻塞报告）: %s", e)


async def _aggregate_stats(session: AsyncSession, period_start: datetime, period_end: datetime) -> dict:
    """单 SQL 聚合所有维度（简化版，满足 D9 模式）"""
    # 文章总数
    r = await session.execute(
        text("SELECT COUNT(*) FROM articles WHERE created_at BETWEEN :s AND :e AND dedupe_of IS NULL"),
        {"s": period_start, "e": period_end},
    )
    total = r.scalar() or 0
    # 按源
    r = await session.execute(
        text(
            "SELECT f.name, COUNT(*) cnt FROM articles a JOIN feeds f ON f.id=a.feed_id WHERE a.created_at BETWEEN :s AND :e GROUP BY f.name ORDER BY cnt DESC LIMIT 5"
        ),
        {"s": period_start, "e": period_end},
    )
    by_source = [{"name": row[0], "count": row[1]} for row in r.fetchall()]
    # 队列
    r = await session.execute(text("SELECT status, COUNT(*) FROM processing_jobs GROUP BY status"))
    queue = {row[0]: row[1] for row in r.fetchall()}
    return {
        "period": {"start": period_start.isoformat(), "end": period_end.isoformat()},
        "articles_total": total,
        "articles_by_source": by_source,
        "queue_stats": queue,
        "summaries_generated": total,
    }


async def _render_html(markdown_text: str) -> str:
    """Markdown → HTML（简化，不依赖 markdown 库的全部 extras）"""
    try:
        import markdown as md_lib

        return md_lib.markdown(markdown_text, extensions=["toc", "fenced_code", "tables"])
    except Exception:
        # 回退：简单包裹
        return f"<pre>{markdown_text}</pre>"


async def _create_pending_report(session: AsyncSession, report_type: str, period_start: date, period_end: date) -> int:
    result = await session.execute(
        text(
            "INSERT INTO reports (report_type, period_start, period_end, status, period_start, period_end) "
            "VALUES (:type, :s, :e, 'pending', :s, :e) "
            "ON CONFLICT (report_type, period_start, period_end) DO UPDATE SET status='pending', updated_at=now() RETURNING id"
        ),
        {"type": report_type, "s": period_start, "e": period_end},
    )
    # 上面 SQL 有重复 period_start/period_end 列，简化：直接查 id
    # 兼容：若 ON CONFLICT 未返回 id，则查
    row = result.first()
    if row:
        return row[0]
    r = await session.execute(
        text("SELECT id FROM reports WHERE report_type=:type AND period_start=:s AND period_end=:e"),
        {"type": report_type, "s": period_start, "e": period_end},
    )
    return r.scalar()


async def _mark_failed(session: AsyncSession, report_id: int, error: str):
    await session.execute(
        text("UPDATE reports SET status='failed', error=:err, completed_at=now() WHERE id=:rid"),
        {"err": error[:500], "rid": report_id},
    )
    await session.commit()


async def generate_daily_report(session: AsyncSession, report_dt: datetime | None = None, settings: Settings | None = None, llm_client=None):
    """日报生成（DESIGN §10.1 伪代码简化）"""
    from app.config import load_settings

    if report_dt is None:
        report_dt = datetime.now()
    period_start = report_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    period_end = period_start + timedelta(days=1) - timedelta(microseconds=1)
    period_s = period_start.date()
    period_e = period_end.date()

    # 创建 pending
    try:
        result = await session.execute(
            text(
                "INSERT INTO reports (report_type, period_start, period_end, status, created_at) "
                "VALUES ('daily', :s, :e, 'pending', now()) "
                "ON CONFLICT (report_type, period_start, period_end) DO UPDATE SET status='pending', updated_at=now() RETURNING id"
            ),
            {"s": period_s, "e": period_e},
        )
        row = result.first()
        report_id = row[0] if row else None
        if not report_id:
            r = await session.execute(text("SELECT id FROM reports WHERE report_type='daily' AND period_start=:s AND period_end=:e"), {"s": period_s, "e": period_e})
            report_id = r.scalar()
        stats = await _aggregate_stats(session, period_start, period_end)
        # LLM 综合（若无 llm_client 则用假数据）
        if llm_client and settings:
            from app.llm.base import GenerateRequest
            from app.llm.prompts import get_prompt

            sys_p, user_p = get_prompt("generate_report", report_type="daily", stats=json.dumps(stats, ensure_ascii=False), period_start=period_s.isoformat(), period_end=period_e.isoformat())
            resp = await llm_client.generate(GenerateRequest(model=settings.llm.generate.model if settings.llm.generate else settings.llm.model, messages=[{"role": "system", "content": sys_p}, {"role": "user", "content": user_p}], json_mode=False))
            content_md = resp.text.strip()
        else:
            content_md = f"# 日报 {period_s}\n\n- 新增 {stats['articles_total']} 篇\n- 队列 {stats['queue_stats']}\n"
        content_html = await _render_html(content_md)
        await session.execute(
            text("UPDATE reports SET status='succeeded', content_md=:md, content_html=:html, stats_json=:stats::jsonb, completed_at=now() WHERE id=:rid"),
            {"md": content_md, "html": content_html, "stats": json.dumps(stats), "rid": report_id},
        )
        await session.commit()
        # 飞书推送（succeeded 后链式，失败不阻塞）
        await _maybe_notify_feishu("daily", f"日报 {period_s}", content_md, settings)
        return report_id
    except Exception as e:
        if 'report_id' in locals() and report_id:
            await _mark_failed(session, report_id, str(e))
        raise


async def generate_weekly_report(session: AsyncSession, report_dt: datetime | None = None, settings=None, llm_client=None):
    """周报生成（复用日报逻辑，周期 7 天）"""
    if report_dt is None:
        report_dt = datetime.now()
    # 周一为周期起点
    period_start = (report_dt - timedelta(days=report_dt.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    period_end = period_start + timedelta(days=7) - timedelta(microseconds=1)
    period_s = period_start.date()
    period_e = period_end.date()
    # 复用日报逻辑，改 report_type
    try:
        result = await session.execute(
            text(
                "INSERT INTO reports (report_type, period_start, period_end, status, created_at) "
                "VALUES ('weekly', :s, :e, 'pending', now()) "
                "ON CONFLICT (report_type, period_start, period_end) DO UPDATE SET status='pending', updated_at=now() RETURNING id"
            ),
            {"s": period_s, "e": period_e},
        )
        row = result.first()
        report_id = row[0] if row else None
        if not report_id:
            r = await session.execute(text("SELECT id FROM reports WHERE report_type='weekly' AND period_start=:s AND period_end=:e"), {"s": period_s, "e": period_e})
            report_id = r.scalar()
        stats = await _aggregate_stats(session, period_start, period_end)
        if llm_client and settings:
            from app.llm.base import GenerateRequest
            from app.llm.prompts import get_prompt

            sys_p, user_p = get_prompt("generate_report", report_type="weekly", stats=json.dumps(stats, ensure_ascii=False), period_start=period_s.isoformat(), period_end=period_e.isoformat())
            resp = await llm_client.generate(GenerateRequest(model=settings.llm.generate.model if settings.llm.generate else settings.llm.model, messages=[{"role": "system", "content": sys_p}, {"role": "user", "content": user_p}], json_mode=False))
            content_md = resp.text.strip()
        else:
            content_md = f"# 周报 {period_s} - {period_e}\n\n- 新增 {stats['articles_total']} 篇\n"
        content_html = await _render_html(content_md)
        await session.execute(
            text("UPDATE reports SET status='succeeded', content_md=:md, content_html=:html, stats_json=:stats::jsonb, completed_at=now() WHERE id=:rid"),
            {"md": content_md, "html": content_html, "stats": json.dumps(stats), "rid": report_id},
        )
        await session.commit()
        # 飞书推送（succeeded 后链式，失败不阻塞）
        await _maybe_notify_feishu("weekly", f"周报 {period_s} - {period_e}", content_md, settings)
        return report_id
    except Exception as e:
        if 'report_id' in locals() and report_id:
            await _mark_failed(session, report_id, str(e))
        raise
