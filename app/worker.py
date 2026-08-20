"""Worker 入口 — Phase 1 单进程 asyncio loop（DESIGN §6 运维模式）

python -m app.worker 启动全套：worker task + APScheduler。
"""

from __future__ import annotations

import asyncio
import logging
import signal

from app.config import load_settings
from app.db.engine import check_extensions, dispose_engine
from app.llm.client import LLMClient, PermanentError
from app.llm.factory import build_provider
from app.pipeline import worker_loop
from app.scheduler import setup_scheduler
from app.services.llm_tasks import run_embed_core, run_embed_summary, run_summarize, run_translate
from app.services.topics import classify_topics
from app.services.wiki import generate_article_wiki
from sqlalchemy.ext.asyncio import AsyncSession

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# task → capability 映射（fix #7 同步扩展 Phase 2 任务）
_TASK_CAPABILITY: dict[str, str] = {
    "embed_core": "embed",
    "embed_summary": "embed",
    "summarize": "generate",
    "topics": "generate",
    "wiki": "generate",
    "extract_entities": "generate",
    "generate_entity_wiki": "generate",
    "generate_topic_wiki": "generate",
    "translate": "generate",
}


# 薄壳函数把 services 的 (article_id, settings) 接口适配到 worker 派发器的 (session, job, settings, llm_client)
async def run_classify_topics(
    session: AsyncSession,
    job: dict,
    settings,
    llm_client: LLMClient | None,
) -> None:
    """topics 任务 handler: 调用 services.topics.classify_topics + check_and_set_done。"""
    if not llm_client:
        raise RuntimeError("classify_topics 需要 llm_client（generate capability）")
    await classify_topics(session, job["article_id"], settings, llm_client)


async def run_generate_wiki(
    session: AsyncSession,
    job: dict,
    settings,
    llm_client: LLMClient | None,
) -> None:
    """wiki 任务 handler: 调用 services.wiki.generate_article_wiki（无 LLM 调用，llm_client 可为 None）。"""
    await generate_article_wiki(session, job["article_id"], settings)


async def run_translate_wrapper(
    session: AsyncSession,
    job: dict,
    settings,
    llm_client: LLMClient | None,
) -> None:
    """translate 任务 handler（DESIGN §6.Z，本地 27B 后台慢任务）"""
    from app.services.llm_tasks import run_translate

    if not llm_client:
        raise RuntimeError("translate 需要 llm_client（generate capability）")
    await run_translate(session, job, settings, llm_client)


async def run_extract_entities(
    session: AsyncSession, job: dict, settings, llm_client: LLMClient | None
) -> None:
    """extract_entities handler（DESIGN §6.Y）"""
    from app.services.entities import extract_entities

    if not llm_client:
        raise RuntimeError("extract_entities 需要 llm_client")
    await extract_entities(session, job["article_id"], settings, llm_client)


async def _run_generate_entity_wiki(session: AsyncSession, job: dict, settings, llm_client=None):
    from app.services.wiki import generate_article_wiki

    await generate_article_wiki(session, job["article_id"], settings)


async def _run_generate_topic_wiki(session: AsyncSession, job: dict, settings, llm_client=None):
    from app.services.wiki import generate_article_wiki

    await generate_article_wiki(session, job["article_id"], settings)


async def main() -> None:
    """启动 worker。"""
    settings = load_settings()
    logger.info("加载配置: db=%s", settings.db.dsn.split("@")[-1])

    # 校验扩展
    try:
        await check_extensions(settings)
    except RuntimeError as e:
        logger.error("数据库校验失败: %s", e)
        return

    # 构建 per-capability LLM client（独立信号量，embed 不被 generate 阻塞）
    gen_provider = build_provider("generate", settings)
    emb_provider = build_provider("embed", settings)
    _gen_cfg = settings.llm.generate
    generate_llm = LLMClient(
        provider=gen_provider,
        max_concurrency=(_gen_cfg.max_concurrency if _gen_cfg else settings.llm.max_concurrency),
    )
    # EmbedSettings 没有 max_concurrency 字段，退回顶层配置
    embed_llm = LLMClient(
        provider=emb_provider,
        max_concurrency=settings.llm.max_concurrency,
    )

    # 健康探测（探测 generate 端点即可）
    gen = settings.llm.generate
    gen_backend = gen.backend if gen else settings.llm.backend
    gen_model = gen.model if gen else settings.llm.model
    emb_backend = settings.llm.embed.backend
    status = await generate_llm.healthcheck()
    endpoint = gen.endpoint if gen else settings.llm.endpoint
    if status.healthy:
        logger.info(
            "✅ LLM 健康: generate=%s(%s) embed=%s @ %s (%dms)",
            gen_backend, gen_model, emb_backend, endpoint, status.latency_ms,
        )
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

    # task dispatcher：闭包捕获 generate_llm / embed_llm
    _clients: dict[str, LLMClient] = {
        "generate": generate_llm,
        "embed": embed_llm,
    }

    async def task_dispatcher(
        session: AsyncSession,
        job: dict,
        settings,
        llm_client: LLMClient | None,
    ) -> None:
        """任务分发器：按 task 类型路由到对应 LLMClient。"""
        task = job["task"]
        capability = _TASK_CAPABILITY.get(task)
        client = _clients.get(capability, llm_client) if capability else llm_client
        handlers = {
            "embed_core": run_embed_core,
            "embed_summary": run_embed_summary,
            "summarize": run_summarize,
            "topics": run_classify_topics,
            "wiki": run_generate_wiki,
            "translate": run_translate_wrapper,
            "extract_entities": run_extract_entities,
            "generate_entity_wiki": _run_generate_entity_wiki,
            "generate_topic_wiki": _run_generate_topic_wiki,
        }
        handler = handlers.get(task)
        if not handler:
            # 直接 return 会被 process_job_with_lease_renewal 当成成功、
            # 把未知任务标成 succeeded。必须抛永久错误进死信。
            raise PermanentError(f"未知任务类型: {task} (job {job['id']})")
        await handler(session, job, settings, client)

    # 启动 worker
    worker_task = asyncio.create_task(
        worker_loop(settings, llm_client=generate_llm, task_handler=task_dispatcher)
    )

    # 启动 APScheduler：fetch_all / drain_queue / healthcheck / pg_backup / cleanup（DESIGN §6/§10）
    scheduler = setup_scheduler(settings, llm_client=generate_llm)
    scheduler.start()
    logger.info(
        "✅ APScheduler 已启动: fetch_all(%dh) / drain_queue(30s) / healthcheck(5m) / pg_backup(03:00) / cleanup(04:00)",
        settings.ingestion.fetch_interval_hours,
    )

    # 等待退出信号
    await stop_event.wait()
    scheduler.shutdown(wait=False)
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass

    await dispose_engine()
    logger.info("worker 已退出")


if __name__ == "__main__":
    asyncio.run(main())
