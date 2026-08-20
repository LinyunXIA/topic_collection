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

# 任务优先级（DESIGN §6，fix #7 扩展 Phase 2 任务，fix #25 对齐 translate 7）
TASK_PRIORITY = {
    "embed_core": 1,
    "summarize": 2,
    "topics": 3,
    "extract_entities": 3,
    "wiki": 4,
    "translate": 7,
    "generate_entity_wiki": 5,
    "generate_topic_wiki": 5,
    "embed_summary": 6,
}

# 瞬时错误的阶梯退避，封顶 15m（DESIGN §6/§11）
TRANSIENT_BACKOFFS = ["1 minute", "5 minutes", "15 minutes"]


def _transient_backoff(step: int) -> str:
    """按已失败次数取退避档位，超出则停在最后一档（封顶）。"""
    if step < 0:
        step = 0
    return TRANSIENT_BACKOFFS[min(step, len(TRANSIENT_BACKOFFS) - 1)]


# ── 入队 ──────────────────────────────────────────────────────────

async def enqueue_jobs(
    session: AsyncSession,
    article_id: int,
    tasks: list[str],
    content_hash: str,
) -> None:
    """幂等入队：ON CONFLICT DO NOTHING + supersede 同事务。

    同一事务内把 article.status 从 pending 升到 processing（idempotent）：
    - 由 scheduler.py / services/cli.py / complete_summarize 等多处入队时统一触发
    - 由 check_and_set_done 检查 status='processing' 才置 done
    - 不存在 → done 永远不触发

    Args:
        session: async session（调用方负责事务边界）
        article_id: 文章 ID
        tasks: 要入队的任务列表（如 ["embed_core", "summarize"]）
        content_hash: 文章当前内容版本
    """
    # 文章状态机：pending → processing（首次入队时升级，幂等）
    await session.execute(
        text(
            "UPDATE articles SET status='processing' "
            "WHERE id=:aid AND status='pending'"
        ),
        {"aid": article_id},
    )
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

        backoff = _transient_backoff(timeouts - 1)
        await session.execute(
            text(
                "UPDATE processing_jobs "
                "SET status='queued', error_class='transient', "
                "consecutive_timeouts=:timeouts, error=:err, "
                f"lock_until=now() + INTERVAL '{backoff}', updated_at=now() "
                "WHERE id=:jid AND status='running'"
            ),
            {"jid": job_id, "timeouts": timeouts, "err": error_msg},
        )
    else:
        # 非超时的瞬时错误：必须把 consecutive_timeouts 清零，
        # 否则 "连续超时" 计数会跨越中间的非超时失败继续累加，
        # 病态文章判定（3 次连续超时转死信）就会误伤正常任务。
        await session.execute(
            text(
                "UPDATE processing_jobs "
                "SET status='queued', error_class='transient', error=:err, "
                "consecutive_timeouts=0, "
                f"lock_until=now() + INTERVAL '{TRANSIENT_BACKOFFS[0]}', updated_at=now() "
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
    """检查文章是否可置 done（DESIGN §6 状态机，§6.Z translate 后台不阻塞）。"""
    result = await session.execute(
        text(
            "SELECT NOT EXISTS ("
            "  SELECT 1 FROM processing_jobs "
            "  WHERE article_id=:aid AND status IN ('queued','running') AND task != 'translate'"
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

async def recover_interrupted(
    session_factory,
    *,
    force_all_running: bool = False,
) -> int:
    """回收过期 / 孤立的 running job（DESIGN §6）。

    Args:
        session_factory: async session factory
        force_all_running: True → 抢所有 status='running' 的 lease（启动期单 worker 假设，
                            处理前 worker 被强杀、lease 尚未过期的场景）；
                          False（默认）→ 仅回收 lock_until < now() 的过期 lease（运行期安全）。

    Phase 1 单 worker 假设由 CLAUDE.md 锁定：启动时可用 force_all_running=True 抢锁，
    周期回收维持 force_all_running=False 防止双 worker 误抢。

    不动 error 字段，recover_count 追踪回收次数。
    """
    where_clause = (
        "WHERE status='running'"
        if force_all_running
        else "WHERE status='running' AND lock_until < now()"
    )
    async with session_factory() as session:
        result = await session.execute(
            text(
                "UPDATE processing_jobs "
                "SET status='queued', lock_until=NULL, "
                "    recover_count=recover_count+1, updated_at=now() "
                f"{where_clause} "
                "RETURNING id"
            )
        )
        recovered = result.fetchall()
        await session.commit()
        if recovered:
            label = "全部 running（强制）" if force_all_running else "过期 lease"
            logger.info("recover: 回收 %d 个 %s 的 job", len(recovered), label)
        return len(recovered)


# ── Lease 续租（处理任务时跑） ──────────────────────────────────────

async def _lease_renewer(
    session_factory,
    job_id: int,
    stop_event: asyncio.Event,
    *,
    interval_s: int = 60,
) -> None:
    """每 interval_s 秒续租 lease（DESIGN §6 随处理协程，不另起 watchdog）。

    stop_event.set() 后下一次 wait_for 立即返回，平滑退出。
    续租 SQL 失败仅 warning，不抛——lease 自然过期由 worker_loop 周期 recover 兜底。
    """
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
            return  # 正常停止
        except asyncio.TimeoutError:
            try:
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
            except Exception as e:
                logger.warning("renew_lease job=%d 失败: %s", job_id, e)


async def process_job_with_lease_renewal(
    session_factory,
    job: dict,
    settings: Settings,
    task_handler,
    llm_client: LLMClient | None,
) -> None:
    """处理单个 job + 后台 lease 续租（与 LLM 调用并行）。

    续租 task 跑独立 session，不争 handler 的 session；
    异常分类（permanent/transient/failed）与原 worker_loop 行为一致。

    成功路径额外负责把 processing_jobs.status='running' → 'succeeded'，然后
    check_and_set_done 检查是否文章可升 done。原来只有 complete_embed handler
    自己清状态（summarize / topics / wiki 的 handler 不清），导致 job 永久
    占用 'running' 计数、阻塞后续 pick_and_claim（lock_until 在 renewer
    续命下保持有效）。
    """
    renew_stop = asyncio.Event()
    renew_task = asyncio.create_task(
        _lease_renewer(session_factory, job["id"], renew_stop)
    )
    try:
        async with session_factory() as session:
            try:
                if task_handler:
                    await task_handler(session, job, settings, llm_client)
                    # 关键修复：handler 成功返回后必须把 job 标 succeeded，
                    # 否则 summarize/topics/wiki 这类"轻 handler"会把
                    # status='running' 留到天荒地老，renewer 还在续 lease。
                    await session.execute(
                        text(
                            # 必须带 status='running' 守卫：job 可能已被
                            # 近似去重或并发 enqueue 置为 'superseded'，
                            # 无守卫会把它复活成 'succeeded'
                            "UPDATE processing_jobs "
                            "SET status='succeeded', lock_until=NULL, updated_at=now() "
                            "WHERE id=:jid AND status='running'"
                        ),
                        {"jid": job["id"]},
                    )
                    await check_and_set_done(session, job["article_id"])
                    await session.commit()
                else:
                    await handle_permanent_failure(
                        session, job["id"], "no task_handler registered"
                    )
                    await check_and_set_done(session, job["article_id"])
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
                await check_and_set_done(session, job["article_id"])
                await session.commit()
    finally:
        renew_stop.set()
        # 给 renewer 最多 2s 平滑退出（renewer 每 60s 才打一次 SQL，正常 0ms 内退出）
        try:
            await asyncio.wait_for(renew_task, timeout=2)
        except asyncio.TimeoutError:
            renew_task.cancel()
            try:
                await renew_task
            except (asyncio.CancelledError, Exception):
                pass
        except asyncio.CancelledError:
            pass


# ── Worker 主循环 ──────────────────────────────────────────────────

# worker_loop 周期回收间隔（秒）
WORKER_RECOVER_PERIOD_S = 60
# LLM 不健康时的重探间隔（秒）
HEALTH_RECHECK_PERIOD_S = 5


async def worker_loop(
    settings: Settings,
    llm_client: LLMClient | None = None,
    task_handler=None,
) -> None:
    """常驻自驱 worker 循环（DESIGN §6）。

    循环：周期 recover → 领取(SKIP LOCKED) → 无任务 sleep ~1s → 处理（含后台续租）→ 继续。
    领取门控：不 healthy 则不领新 job。
    启动期 force 回收所有 running lease 处理"前 worker 被强杀"场景（Phase 1 单 worker 假设）。
    """
    from app.db.engine import get_session_factory

    session_factory = get_session_factory(settings)

    # 启动期强制回收（Phase 1 单 worker：claude.md 锁定）
    recovered = await recover_interrupted(session_factory, force_all_running=True)
    if recovered:
        logger.warning(
            "启动强制回收 %d 个 running job（Phase1 单 worker 假设）", recovered,
        )

    logger.info("worker 启动，开始消费队列")
    last_recover_at = asyncio.get_event_loop().time()

    while True:
        try:
            # 周期回收（仅过期 lease，多 worker 安全）
            now_t = asyncio.get_event_loop().time()
            if now_t - last_recover_at >= WORKER_RECOVER_PERIOD_S:
                await recover_interrupted(session_factory)
                last_recover_at = now_t

            # 领取门控：不 healthy 则退避，并重新探测。
            # 只 sleep 不重探的话，启动时 LLM 恰好不可用就会永久空转
            # ——healthy 只有 healthcheck() 会写，没人再调它（PRD 验收 7）。
            if llm_client and not llm_client.is_healthy:
                await asyncio.sleep(HEALTH_RECHECK_PERIOD_S)
                try:
                    status = await llm_client.healthcheck()
                    if status.healthy:
                        logger.info("LLM 恢复可用，继续消费队列")
                    else:
                        logger.debug("LLM 仍不可用: %s", status.error)
                except Exception as e:
                    logger.debug("healthcheck 异常: %s", e)
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

            # 处理任务（带后台 lease 续租）
            await process_job_with_lease_renewal(
                session_factory, job, settings, task_handler, llm_client,
            )

        except asyncio.CancelledError:
            logger.info("worker 收到取消信号，退出")
            break
        except Exception as e:
            logger.error("worker 循环异常: %s", e, exc_info=True)
            await asyncio.sleep(5)
