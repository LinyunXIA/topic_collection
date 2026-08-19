"""Worker 入口 — Phase 1 单进程 asyncio loop（DESIGN §6 运维模式）

python -m app.worker 启动全套：worker task + APScheduler。
"""

from __future__ import annotations

import asyncio
import logging
import signal

from app.config import load_settings
from app.db.engine import check_extensions, dispose_engine, get_engine
from app.llm.client import LLMClient
from app.llm.omlx import OMLXProvider
from app.pipeline import worker_loop
from app.services.llm_tasks import run_embed_core, run_embed_summary, run_summarize
from sqlalchemy.ext.asyncio import AsyncSession

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


async def task_dispatcher(
    session: AsyncSession,
    job: dict,
    settings,
    llm_client: LLMClient | None,
) -> None:
    """任务分发器：按 task 类型调用对应处理器。"""
    task = job["task"]
    handlers = {
        "embed_core": run_embed_core,
        "embed_summary": run_embed_summary,
        "summarize": run_summarize,
    }
    handler = handlers.get(task)
    if not handler:
        logger.warning("未知任务类型: %s (job %d)，标记跳过", task, job["id"])
        return
    await handler(session, job, settings, llm_client)


async def main() -> None:
    """启动 worker。"""
    settings = load_settings()
    logger.info("加载配置: db=%s", settings.db.dsn.split("@")[-1])

    # 校验扩展
    engine = get_engine(settings)
    try:
        await check_extensions(settings)
    except RuntimeError as e:
        logger.error("数据库校验失败: %s", e)
        return

    # 构建 LLM client
    provider = OMLXProvider(
        base_url=settings.llm.endpoint,
        generation_model=settings.llm.model,
        embedding_model=settings.llm.embed.model,
        rerank_model=settings.llm.rerank.model,
    )
    llm_client = LLMClient(
        provider=provider,
        max_concurrency=settings.llm.max_concurrency,
    )

    # 健康探测
    status = await llm_client.healthcheck()
    if status.healthy:
        logger.info("✅ LLM 健康: %s (%dms)", settings.llm.endpoint, status.latency_ms)
    else:
        logger.warning("⚠️  LLM 不可用: %s — 将在运行时重试", status.error)

    # 信号处理
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _signal_handler():
        logger.info("收到退出信号")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    # 启动 worker
    worker_task = asyncio.create_task(
        worker_loop(settings, llm_client=llm_client, task_handler=task_dispatcher)
    )

    # 等待退出信号
    await stop_event.wait()
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass

    await dispose_engine()
    logger.info("worker 已退出")


if __name__ == "__main__":
    asyncio.run(main())
