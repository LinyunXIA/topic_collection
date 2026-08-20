"""定时任务 — DESIGN §10

Phase 1 单进程下随 worker 常驻（AsyncIOScheduler 同 asyncio loop）。
- fetch_all: 每 fetch_interval_hours 遍历 enabled feeds
- drain_queue: 每 30s 清理 superseded/死信 + 补入队
- pg_backup: 每日 03:00 pg_dump | gzip
- cleanup_fetch_events: 每日 04:00 清理旧审计记录
- healthcheck: 每 5m LLM 健康探测
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text

logger = logging.getLogger(__name__)


async def fetch_all(settings) -> None:
    """遍历所有 enabled feeds 抓取（DESIGN §10）。"""
    from app.db.engine import get_session_factory
    from app.db.fts import update_article_tsv
    from app.ingest.feeds import FeedFetcher
    from app.ingest.dedup import url_hash, content_hash
    from app.services.cleaner import clean_article
    from app.services.topics import match_keywords
    from app.pipeline import enqueue_jobs

    fetcher = FeedFetcher(settings)
    factory = get_session_factory(settings)

    async with factory() as session:
        result = await session.execute(
            text("SELECT id, name, url, etag, last_modified FROM feeds WHERE enabled=true")
        )
        feeds = result.mappings().all()

    if not feeds:
        logger.debug("fetch_all: 无 enabled feeds")
        return

    total_new = 0
    for feed in feeds:
        try:
            items, new_etag, new_lm = await fetcher.fetch_feed(
                feed_id=feed["id"],
                url=feed["url"],
                etag=feed.get("etag"),
                last_modified=feed.get("last_modified"),
            )

            async with factory() as session:
                new_count = 0
                for item in items:
                    uh = url_hash(item.source_url)
                    ch = content_hash(item.content_text)

                    # 幂等检查：已存在则累计 mention_count（与 cli._feeds_fetch 保持一致）
                    existing = await session.execute(
                        text("SELECT id FROM articles WHERE url_hash=:uh"), {"uh": uh}
                    )
                    existing_row = existing.first()
                    if existing_row:
                        await session.execute(
                            text(
                                "UPDATE articles SET mention_count=mention_count+1 "
                                "WHERE id=:aid"
                            ),
                            {"aid": existing_row[0]},
                        )
                        continue

                    cleaned = await clean_article(item.content_html or item.content_text, item.title)
                    status = "unparseable" if not cleaned["is_parseable"] else "pending"

                    await session.execute(
                        text(
                            "INSERT INTO articles (feed_id, source_url, url_hash, content_hash, "
                            "title, content_text, content_md, lang, word_count, status) "
                            "VALUES (:fid, :url, :uh, :ch, :title, :ct, :cm, :lang, :wc, :status) "
                            "ON CONFLICT (url_hash) DO NOTHING RETURNING id"
                        ),
                        {
                            "fid": feed["id"], "url": item.source_url, "uh": uh, "ch": ch,
                            "title": item.title, "ct": cleaned["content_text"],
                            "cm": cleaned["content_md"], "lang": cleaned["lang"],
                            "wc": cleaned["word_count"], "status": status,
                        },
                    )

                    art_result = await session.execute(
                        text("SELECT id FROM articles WHERE url_hash=:uh"), {"uh": uh}
                    )
                    art_row = art_result.first()
                    if art_row:
                        # tsv 阶段一（DESIGN §5.3）——与 cli._feeds_fetch 保持一致
                        await update_article_tsv(
                            session, art_row[0],
                            title=item.title or "",
                            content_text=cleaned["content_text"] or "",
                        )
                    if art_row and status == "pending":
                        await enqueue_jobs(session, art_row[0], ["embed_core", "summarize"], ch)
                        # 关键词快路径：不做这步的话 complete_summarize 里 has_keyword 恒为
                        # false，每篇文章都会白烧一次 LLM 分类（PRD 验收 3）
                        await match_keywords(session, art_row[0])
                    new_count += 1

                await session.execute(
                    text(
                        "UPDATE feeds SET etag=:etag, last_modified=:lm, "
                        "last_fetched_at=now(), fetch_status='ok', fetch_failures=0 "
                        "WHERE id=:fid"
                    ),
                    {"etag": new_etag, "lm": new_lm, "fid": feed["id"]},
                )
                await session.commit()
                total_new += new_count
                if new_count:
                    logger.info("fetch_all: %s + %d 篇", feed["name"], new_count)

        except Exception as e:
            logger.error("fetch_all: %s 失败: %s", feed["name"], e)
            async with factory() as session:
                await session.execute(
                    text(
                        "UPDATE feeds SET fetch_failures=fetch_failures+1, "
                        "last_error=:err, fetch_status='error' WHERE id=:fid"
                    ),
                    {"err": str(e)[:500], "fid": feed["id"]},
                )
                await session.commit()

    if total_new:
        logger.info("fetch_all 完成: + %d 篇", total_new)


async def drain_queue(settings) -> None:
    """维护队列：清理 superseded + 死信（DESIGN §6/§10）。

    不参与领取（worker 常驻自驱）。
    """
    from app.db.engine import get_session_factory

    factory = get_session_factory(settings)
    async with factory() as session:
        # 清理 superseded（超过 1 小时的）
        result = await session.execute(
            text(
                "DELETE FROM processing_jobs "
                "WHERE status = 'superseded' AND updated_at < now() - INTERVAL '1 hour'"
            )
        )
        superseded_cleaned = result.rowcount

        # 清理已成功的（超过 24 小时的）
        result = await session.execute(
            text(
                "DELETE FROM processing_jobs "
                "WHERE status = 'succeeded' AND updated_at < now() - INTERVAL '24 hours'"
            )
        )
        succeeded_cleaned = result.rowcount

        await session.commit()

    if superseded_cleaned or succeeded_cleaned:
        logger.debug(
            "drain_queue: 清理 %d superseded + %d succeeded",
            superseded_cleaned, succeeded_cleaned,
        )


async def cleanup_fetch_events(settings) -> None:
    """清理旧 fetch_events（DESIGN §10）。"""
    from app.db.engine import get_session_factory

    retention_days = settings.ingestion.fetch_events_retention_days
    factory = get_session_factory(settings)
    async with factory() as session:
        result = await session.execute(
            text(
                "DELETE FROM fetch_events "
                "WHERE created_at < now() - INTERVAL ':days days'"
            ),
            {"days": retention_days},
        )
        await session.commit()
        if result.rowcount:
            logger.info("cleanup_fetch_events: 清理 %d 条旧记录", result.rowcount)


async def healthcheck(settings, llm_client=None) -> None:
    """LLM 健康探测（DESIGN §10，每 5m）。"""
    if not llm_client:
        return
    try:
        status = await llm_client.healthcheck()
        llm_client.healthy = status.healthy
        if not status.healthy:
            logger.warning("healthcheck: LLM 不可用: %s", status.error)
    except Exception as e:
        logger.error("healthcheck 异常: %s", e)
        llm_client.healthy = False


def run_pg_backup(settings) -> None:
    """pg_dump 备份（DESIGN §10，每日 03:00）。

    走 docker compose exec，宿主机不一定有 pg_dump。
    """
    backup_dir = Path(settings.data_dir) / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = backup_dir / f"tc-{timestamp}.sql.gz"

    try:
        result = subprocess.run(
            [
                "docker", "compose", "exec", "-T", "postgres",
                "pg_dump", "-U", "tc", "-d", "topic_collection",
                "--no-owner", "--no-privileges",
            ],
            capture_output=True,
            timeout=300,
        )
        if result.returncode != 0:
            logger.error("pg_backup: pg_dump 失败: %s", result.stderr.decode())
            return

        import gzip
        with open(backup_file, "wb") as f:
            f.write(gzip.compress(result.stdout))

        size_mb = backup_file.stat().st_size / 1024 / 1024
        logger.info("pg_backup: %s (%.1f MB)", backup_file.name, size_mb)

        # 清理超过 14 天的备份
        cutoff = datetime.now() - timedelta(days=14)
        for old_file in backup_dir.glob("tc-*.sql.gz"):
            if old_file.stat().st_mtime < cutoff.timestamp():
                old_file.unlink()
                logger.debug("pg_backup: 清理旧备份 %s", old_file.name)

    except FileNotFoundError:
        logger.error("pg_backup: docker 未安装")
    except subprocess.TimeoutExpired:
        logger.error("pg_backup: pg_dump 超时")
    except Exception as e:
        logger.error("pg_backup: %s", e)
