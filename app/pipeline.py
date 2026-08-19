"""数据流水线 — 队列入队 + Worker 领取 + 重试分类 + recover（DESIGN §6）

核心职责：
- enqueue_jobs(): 幂等入队 + supersede 同事务
- pick_and_claim(): 单条原子 pick-and-claim（FOR UPDATE SKIP LOCKED）
- worker_loop(): 常驻自驱循环
- recover_interrupted(): 仅 worker 启动时回收过期 running job
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.engine import get_session
from app.llm.client import LLMClient, PermanentError

logger = logging.getLogger(__name__)

# 任务优先级（DESIGN §6）
TASK_PRIORITY = {
    "embed_core": 1,
    "summarize": 2,
    "topics": 3,
    "wiki": 4,
    "translate": 5,
    "embed_summary": 6,
}

# 退避时间（瞬时错误）
TRANSIENT_BACKOFFS = ["1 minute", "5 minutes", "15 minutes"]


# ── 入队 ──────────────────────────────────────────────────────────

async def enqueue_jobs(
    session: AsyncSession,
    article_id: int,
    tasks: list[str],
    content_hash: str,
) -> None:
    """幂等入队：ON CONFLICT DO NOTHING + supersede 同事务。

    Args:
        session: async session（调用方负责事务边界）
        article_id: 文章 ID
        tasks: 要入队的任务列表（如 ["embed_core", "summarize"]）
        content_hash: 文章当前内容版本
    """
    for task in tasks:
        priority = TASK_PRIORITY.get(task, 5)
        # 1. supersede 旧 job（同事务）
        await session.execute(
            text(
                "UPDATE processing_jobs SET status='superseded', updated_at=now() "
                "WHERE article_id=:aid AND task=:task AND status IN ('queued','running')"
            ),
            {"aid": article_id, "task": task},
        )
        # 2. 入队新 job（幂等）
        await session.execute(
            text(
                "INSERT INTO processing_jobs "
                "(article_id, task, status, content_hash, priority) "
                "VALUES (:aid, :task, 'queued', :ch, :pri) "
                "ON CONFLICT DO NOTHING"
            ),
            {"aid": article_id, "task": task, "ch": content_hash, "pri": priority},
        )


# ── Worker 领取 ───────────────────────────────────────────────────

async def pick_and_claim(session: AsyncSession) -> dict | None:
    """单条原子 pick-and-claim（DESIGN §6）。

    FOR UPDATE SKIP LOCKED + UPDATE 同事务。
    领取 SQL 不自增 attempt（attempt 由永久失败路径独占）。
    """
    result = await session.execute(
        text(
            "UPDATE processing_jobs "
            "SET status='running', lock_until=now() + INTERVAL '5 minutes', updated_at=now() "
            "WHERE id = ("
            "  SELECT id FROM processing_jobs "
            "  WHERE status='queued' AND (lock_until IS NULL OR lock_until < now()) "
            "  ORDER BY priority, created_at "
            "  LIMIT 1 "
            "  FOR UPDATE SKIP LOCKED"
            ") "
            "RETURNING id, article_id, task, content_hash, attempt, max_attempts, "
            "          error_class, consecutive_timeouts, payload_json"
        )
    )
    row = result.mappings().first()
    return dict(row) if row else None


# ── 续租 ──────────────────────────────────────────────────────────

async def renew_lease(session_factory, job_id: int) -> None:
    """续租 lock_until（随处理协程，不另起 watchdog）。"""
    async with session_factory() as session:
        await session.execute(
            text(
                "UPDATE processing_jobs "
                "SET lock_until=now() + INTERVAL '5 minutes' "
                "WHERE id=:jid AND status='running'"
            ),
            {"jid": job_id},
        )
        await session.commit()


# ── 失败处理 ──────────────────────────────────────────────────────

async def handle_transient_failure(
    session: AsyncSession,
    job_id: int,
    error_msg: str,
    is_timeout: bool = False,
    health_ok: bool = True,
    max_timeout_retries: int = 3,
) -> None:
    """瞬时错误处理（DESIGN §6）。

    - attempt 不自增（死信预算不消耗）
    - consecutive_timeouts 仅在超时时 +1
    - 超时达阈值且 healthcheck 正常 → 直接 failed 死信
    - 否则 status='queued' + lock_until 退避
    """
    if is_timeout:
        # 获取当前 consecutive_timeouts
        result = await session.execute(
            text("SELECT consecutive_timeouts FROM processing_jobs WHERE id=:jid"),
            {"jid": job_id},
        )
        row = result.first()
        timeouts = (row[0] or 0) + 1 if row else 1

        if timeouts >= max_timeout_retries and health_ok:
            # 超时转永久死信（DESIGN §6: 病态文章直接 failed）
            await session.execute(
                text(
                    "UPDATE processing_jobs "
                    "SET status='failed', error_class='permanent', "
                    "consecutive_timeouts=:timeouts, error=:err, lock_until=NULL, updated_at=now() "
                    "WHERE id=:jid AND status='running'"
                ),
                {"jid": job_id, "timeouts": timeouts, "err": error_msg},
            )
            logger.warning("Job %d 超时转死信 (timeouts=%d)", job_id, timeouts)
            return

        await session.execute(
            text(
                "UPDATE processing_jobs "
                "SET status='queued', error_class='transient', "
                "consecutive_timeouts=:timeouts, error=:err, "
                "lock_until=now() + INTERVAL '15 minutes', updated_at=now() "
                "WHERE id=:jid AND status='running'"
            ),
            {"jid": job_id, "timeouts": timeouts, "err": error_msg},
        )
    else:
        # 连续超时（非首次）重置为 1，否则保持 0
        await session.execute(
            text(
                "UPDATE processing_jobs "
                "SET status='queued', error_class='transient', error=:err, "
                "lock_until=now() + INTERVAL '5 minutes', updated_at=now() "
                "WHERE id=:jid AND status='running'"
            ),
            {"jid": job_id, "err": error_msg},
        )


async def handle_permanent_failure(
    session: AsyncSession,
    job_id: int,
    error_msg: str,
) -> None:
    """永久错误处理（DESIGN §6）。

    - attempt+1
    - attempt >= max_attempts → failed 死信
    - 否则 queued + 短退避
    """
    result = await session.execute(
        text(
            "UPDATE processing_jobs "
            "SET "
            "  status = CASE WHEN attempt+1 >= max_attempts THEN 'failed' ELSE 'queued' END, "
            "  lock_until = CASE WHEN attempt+1 >= max_attempts THEN NULL "
            "                     ELSE now() + INTERVAL '30 seconds' END, "
            "  attempt = attempt + 1, "
            "  error_class = 'permanent', "
            "  error = :err, "
            "  consecutive_timeouts = 0, "
            "  updated_at = now() "
            "WHERE id = :jid AND status = 'running' "
            "RETURNING status"
        ),
        {"jid": job_id, "err": error_msg},
    )
    row = result.first()
    if row and row[0] == "failed":
        logger.warning("Job %d 永久死信: %s", job_id, error_msg)


# ── Done 检查 ─────────────────────────────────────────────────────

async def check_and_set_done(session: AsyncSession, article_id: int) -> None:
    """检查文章是否可置 done（DESIGN §6 状态机）。"""
    result = await session.execute(
        text(
            "SELECT NOT EXISTS ("
            "  SELECT 1 FROM processing_jobs "
            "  WHERE article_id=:aid AND status IN ('queued','running')"
            ")"
        ),
        {"aid": article_id},
    )
    all_done = result.scalar()
    if all_done:
        await session.execute(
            text(
                "UPDATE articles SET status='done' "
                "WHERE id=:aid AND status='processing' AND dedupe_of IS NULL"
            ),
            {"aid": article_id},
        )


# ── Recover ───────────────────────────────────────────────────────

async def recover_interrupted(session_factory) -> int:
    """回收过期 running job（仅 worker 启动时跑，DESIGN §6）。

    不动 error 字段，recover_count 追踪回收次数。
    """
    async with session_factory() as session:
        result = await session.execute(
            text(
                "UPDATE processing_jobs "
                "SET status='queued', lock_until=NULL, "
                "    recover_count=recover_count+1, updated_at=now() "
                "WHERE status='running' AND lock_until < now() "
                "RETURNING id"
            )
        )
        recovered = result.fetchall()
        await session.commit()
        if recovered:
            logger.info("recover: 回收 %d 个过期 running job", len(recovered))
        return len(recovered)


# ── Worker 主循环 ──────────────────────────────────────────────────

async def worker_loop(
    settings: Settings,
    llm_client: LLMClient | None = None,
    task_handler=None,
) -> None:
    """常驻自驱 worker 循环（DESIGN §6）。

    循环：领取(SKIP LOCKED) → 无任务 sleep ~1s → 处理完继续。
    领取门控：不 healthy 则不领新 job。
    """
    from app.db.engine import get_session_factory

    session_factory = get_session_factory(settings)

    # 启动时 recover
    recovered = await recover_interrupted(session_factory)
    if recovered:
        logger.info("启动 recover: 回收 %d 个 job", recovered)

    logger.info("worker 启动，开始消费队列")

    while True:
        try:
            # 领取门控：不 healthy 则 sleep 退避
            if llm_client and not llm_client.is_healthy:
                logger.debug("LLM 不健康，跳过领取，sleep 5s")
                await asyncio.sleep(5)
                continue

            async with session_factory() as session:
                job = await pick_and_claim(session)
                await session.commit()

            if not job:
                await asyncio.sleep(1)
                continue

            logger.info(
                "领取 job %d: article=%d task=%s",
                job["id"], job["article_id"], job["task"],
            )

            # 处理任务（Phase 1 单进程顺序执行，一次一个 job）
            if task_handler:
                async with session_factory() as session:
                    try:
                        await task_handler(session, job, settings, llm_client)
                        await session.commit()
                    except PermanentError as e:
                        await handle_permanent_failure(session, job["id"], str(e))
                        await check_and_set_done(session, job["article_id"])
                        await session.commit()
                    except Exception as e:
                        is_timeout = "timeout" in str(e).lower()
                        await handle_transient_failure(
                            session, job["id"], str(e),
                            is_timeout=is_timeout,
                            health_ok=llm_client.is_healthy if llm_client else True,
                            max_timeout_retries=settings.llm.max_timeout_retries,
                        )
                        await session.commit()
            else:
                # 无 handler 时跳过，标记 failed 防循环
                async with session_factory() as session:
                    await handle_permanent_failure(
                        session, job["id"], "no task_handler registered"
                    )
                    await check_and_set_done(session, job["article_id"])
                    await session.commit()

        except asyncio.CancelledError:
            logger.info("worker 收到取消信号，退出")
            break
        except Exception as e:
            logger.error("worker 循环异常: %s", e, exc_info=True)
            await asyncio.sleep(5)
