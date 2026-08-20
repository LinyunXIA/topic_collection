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

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import text

logger = logging.getLogger(__name__)


async def fetch_all(settings) -> None:
    """遍历所有 enabled feeds 抓取（DESIGN §10）。

    实际抓取逻辑收敛在 app.ingest.service.fetch_and_store；本函数只负责：
      - 选 enabled feeds
      - 错误聚合（写 fetch_failures/last_error）
      - 日志汇总
    """
    from app.db.engine import get_session_factory
    from app.ingest.feeds import FeedFetcher
    from app.ingest.service import fetch_and_store

    fetcher = FeedFetcher(settings)
    factory = get_session_factory(settings)

    # 按 env 隔离（方案 C），兼容旧库无 env 列
    import os

    cur_env = os.environ.get("TC_APP_ENV", getattr(settings, "app_env", "dev"))
    try:
        async with factory() as session:
            result = await session.execute(
                text("SELECT id, name, url, etag, last_modified FROM feeds WHERE enabled=true AND env=:env"),
                {"env": cur_env},
            )
            feeds = result.mappings().all()
    except Exception:
        async with factory() as session:
            result = await session.execute(
                text("SELECT id, name, url, etag, last_modified FROM feeds WHERE enabled=true")
            )
            feeds = result.mappings().all()

    if not feeds:
        logger.debug("fetch_all: 无 enabled feeds (env=%s)", cur_env)
        return

    total_new = 0
    for feed in feeds:
        try:
            async with factory() as session:
                new_count, _ = await fetch_and_store(
                    session, dict(feed), fetcher, count=None,
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
    """维护队列：清理 superseded/死信 + 回灌 pending 文章（DESIGN §6/§10）。

    不参与领取（worker 常驻自驱）。
    回灌逻辑：找到 status='pending' 且无任何 processing_jobs 行的文章，
    补入 embed_core + summarize jobs，防高量 feed 截断后文章永滞留。
    """
    from app.db.engine import get_session_factory
    from app.pipeline import TASK_PRIORITY

    factory = get_session_factory(settings)
    async with factory() as session:
        # ── 清理 superseded（超过 1 小时的） ──
        result = await session.execute(
            text(
                "DELETE FROM processing_jobs "
                "WHERE status = 'superseded' AND updated_at < now() - INTERVAL '1 hour'"
            )
        )
        superseded_cleaned = result.rowcount

        # ── 清理已成功的（超过 24 小时的） ──
        result = await session.execute(
            text(
                "DELETE FROM processing_jobs "
                "WHERE status = 'succeeded' AND updated_at < now() - INTERVAL '24 hours'"
            )
        )
        succeeded_cleaned = result.rowcount

        # ── 回灌 pending 文章（DESIGN §6 backpressure，fix #33 限定本轮） ──
        # 谓词三条件互锁：status='pending' + dedupe_of IS NULL + 零 job 记录
        # fix #33：UPDATE ... RETURNING id 收集本轮回灌 id，避免扫全表 processing
        # 导致已完成但 succeeded 记录被清掉的文章被重复入队（每 24h 循环）。
        result = await session.execute(
            text(
                "UPDATE articles SET status='processing' "
                "WHERE status = 'pending' AND dedupe_of IS NULL "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM processing_jobs j WHERE j.article_id = articles.id"
                ") RETURNING id"
            )
        )
        backfilled_ids = [r[0] for r in result.fetchall()]
        backfilled = len(backfilled_ids)

        if backfilled_ids:
            # 为回灌文章补入 jobs（embed_core priority=1, summarize priority=2）
            # 限定 a.id = ANY(:ids) 避免越界到全表 processing（fix #33）
            # ON CONFLICT DO NOTHING 保证幂等
            # 注意：:task_val 和 :task_match 分开命名，避免 asyncpg 类型推断冲突
            for task, priority in TASK_PRIORITY.items():
                if task not in ("embed_core", "summarize"):
                    continue
                await session.execute(
                    text(
                        "INSERT INTO processing_jobs "
                        "(article_id, task, status, content_hash, priority) "
                        "SELECT a.id, :task_val, 'queued', a.content_hash, :pri "
                        "FROM articles a "
                        "WHERE a.id = ANY(:ids) "
                        "AND NOT EXISTS ("
                        "  SELECT 1 FROM processing_jobs j "
                        "  WHERE j.article_id = a.id AND j.task = :task_match"
                        ")"
                    ),
                    {"ids": backfilled_ids, "task_val": task, "task_match": task, "pri": priority},
                )

        await session.commit()

    if superseded_cleaned or succeeded_cleaned:
        logger.debug(
            "drain_queue: 清理 %d superseded + %d succeeded",
            superseded_cleaned, succeeded_cleaned,
        )
    if backfilled:
        logger.info("drain_queue: 回灌 %d 篇 pending 文章", backfilled)


async def cleanup_fetch_events(settings, session=None) -> None:
    """清理旧 fetch_events（DESIGN §10）。

    session: 可选，外部传入时复用已有 session（测试/调用方控制生命周期）。
    """
    from app.db.engine import get_session_factory

    retention_days = settings.ingestion.fetch_events_retention_days
    own_session = session is None
    if own_session:
        factory = get_session_factory(settings)
        session = await factory().__aenter__()

    try:
        result = await session.execute(
            text(
                f"DELETE FROM fetch_events "
                f"WHERE created_at < now() - INTERVAL '{int(retention_days)} days'"
            ),
        )
        if own_session:
            await session.commit()
        if result.rowcount:
            logger.info("cleanup_fetch_events: 清理 %d 条旧记录", result.rowcount)
    finally:
        if own_session:
            await session.__aexit__(None, None, None)


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


async def run_pg_backup(settings) -> None:
    """pg_dump 备份（DESIGN §10，每日 03:00，DESIGN §5.4.1 prod 分支）。

    dev 走 docker compose exec，prod 走本机 pg_dump -h localhost -U postgres。
    使用 asyncio.to_thread 避免 subprocess.run 阻塞事件循环。
    """
    import os

    backup_dir = Path(settings.data_dir) / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = backup_dir / f"tc-{timestamp}.sql.gz"

    # 选择 pg_dump 命令：prod 本机 postgres，dev 走 docker（DESIGN §5.4.1）
    if getattr(settings, "app_env", "dev") == "prod":
        cmd = [
            "pg_dump",
            "-h",
            "localhost",
            "-U",
            "postgres",
            "-d",
            "topic_collection",
            "--no-owner",
            "--no-privileges",
        ]
        env = {**os.environ, "PGPASSWORD": os.environ.get("POSTGRES_PASSKEY", "")}
    else:
        cmd = [
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "pg_dump",
            "-U",
            "tc",
            "-d",
            "topic_collection",
            "--no-owner",
            "--no-privileges",
        ]
        env = None

    try:
        result = await asyncio.to_thread(
            subprocess.run,
            cmd,
            capture_output=True,
            timeout=300,
            env=env,
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


def setup_scheduler(settings, llm_client=None) -> AsyncIOScheduler:
    """创建并配置 APScheduler，返回未启动的 scheduler 实例（DESIGN §6/§10）。

    Phase 1 单进程：worker.py 在 main() 中调用此函数，scheduler 与 worker
    共享同一 asyncio 事件循环，无需额外进程。

    注意：必须直接注册**协程函数**本身——AsyncIOScheduler 看到原生协程函数
    会直接在 event loop 中调度；若用 `lambda: asyncio.ensure_future(coro())`
    这种同步包装，APScheduler 会把 lambda 丢进默认 ThreadPoolExecutor 执行，
    而 `asyncio.ensure_future` 在没有 running event loop 的线程里抛
    `RuntimeError: There is no current event loop in thread ...`，
    任务体一次都不会执行（fix #30）。
    """
    scheduler = AsyncIOScheduler()

    scheduler.add_job(
        fetch_all,
        trigger=IntervalTrigger(hours=settings.ingestion.fetch_interval_hours),
        args=(settings,),
        id="fetch_all",
        name="定时抓取全部 enabled feeds",
        replace_existing=True,
    )

    scheduler.add_job(
        drain_queue,
        trigger=IntervalTrigger(seconds=30),
        args=(settings,),
        id="drain_queue",
        name="维护队列：清理 superseded/死信 + 补入队",
        replace_existing=True,
    )

    scheduler.add_job(
        healthcheck,
        trigger=IntervalTrigger(minutes=5),
        args=(settings,),
        kwargs={"llm_client": llm_client},
        id="healthcheck",
        name="LLM 健康探测",
        replace_existing=True,
    )

    scheduler.add_job(
        run_pg_backup,
        trigger=CronTrigger(hour=3, minute=0),
        args=(settings,),
        id="pg_backup",
        name="每日 pg_dump 备份",
        replace_existing=True,
    )

    scheduler.add_job(
        cleanup_fetch_events,
        trigger=CronTrigger(hour=4, minute=0),
        args=(settings,),
        id="cleanup_fetch_events",
        name="清理旧 fetch_events 记录",
        replace_existing=True,
    )

    return scheduler
