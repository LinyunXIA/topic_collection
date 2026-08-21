"""FastAPI 入口 — Phase 2 WebUI（DESIGN §14 2.1.1）

create_app() + lifespan 顺序：init_db → probe oMLX 三端点 → pg_try_advisory_lock → recover_interrupted → 启动 scheduler + worker task
uvicorn app.main:app --host 127.0.0.1 --port 7111
"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import load_settings
from app.db.engine import check_extensions, dispose_engine, get_session_factory
from app.llm.client import LLMClient
from app.llm.factory import build_provider
from app.pipeline import recover_interrupted, worker_loop
from app.scheduler import setup_scheduler

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan: init_db → probe → advisory lock → recover → scheduler+worker"""
    settings = load_settings()
    app.state.settings = settings

    # 1. 校验扩展
    try:
        await check_extensions(settings)
    except RuntimeError as e:
        logger.error("数据库校验失败: %s", e)
        yield
        return

    # 2. 构建 per-capability LLM clients
    gen_provider = build_provider("generate", settings)
    emb_provider = build_provider("embed", settings)
    _gen_cfg = settings.llm.generate
    generate_llm = LLMClient(
        provider=gen_provider,
        max_concurrency=(_gen_cfg.max_concurrency if _gen_cfg else settings.llm.max_concurrency),
    )
    embed_llm = LLMClient(
        provider=emb_provider,
        max_concurrency=settings.llm.max_concurrency,
    )
    app.state.generate_llm = generate_llm
    app.state.embed_llm = embed_llm

    # 3. 健康探测
    try:
        status = await generate_llm.healthcheck()
        logger.info("LLM 健康: %s (%dms)", "ok" if status.healthy else f"fail {status.error}", status.latency_ms)
    except Exception as e:
        logger.warning("LLM 探测异常: %s", e)

    # 4. advisory lock 单例校验（池外长连接，DESIGN §5.4.1 / §14 2.1.1，fix #42）
    # 池外专用长连接持有至进程退出，防止 uvicorn + worker 双活并发
    advisory_conn = None
    try:
        import asyncpg  # type: ignore

        raw_dsn = settings.db.dsn.replace("+asyncpg", "")
        advisory_conn = await asyncpg.connect(raw_dsn)
        locked = await advisory_conn.fetchval(
            "SELECT pg_try_advisory_lock(hashtext('topic_collection_worker'))"
        )
        if not locked:
            logger.error(
                "advisory lock 未获锁，存在另一 worker/webui 进程持有锁，双活风险，强制退出"
            )
            await advisory_conn.close()
            sys.exit(1)
        logger.info("advisory lock 已获取 (hashtext('topic_collection_worker'))")
        app.state.advisory_conn = advisory_conn
    except SystemExit:
        raise
    except Exception as e:
        logger.warning("advisory lock 获取异常: %s (继续启动，单机开发可接受)", e)
        if advisory_conn is not None:
            try:
                await advisory_conn.close()
            except Exception:
                pass
            advisory_conn = None

    # 5. recover
    factory = get_session_factory(settings)
    try:
        recovered = await recover_interrupted(factory, force_all_running=True)
        if recovered:
            logger.warning("启动回收 %d 个 running job", recovered)
    except Exception as e:
        logger.warning("recover 失败: %s", e)

    # 6. 启动 worker + scheduler
    from app.worker import _TASK_CAPABILITY, run_classify_topics, run_generate_wiki, run_translate_wrapper
    from app.services.entities import extract_entities as run_extract_entities
    from app.services.llm_tasks import run_embed_core, run_embed_summary, run_summarize
    from sqlalchemy.ext.asyncio import AsyncSession

    _clients = {"generate": generate_llm, "embed": embed_llm}

    async def _run_generate_entity_wiki(session, job, settings, llm_client=None):
        # Phase 2 占位：按 entity_ids 生成 wiki（简化为复用 article wiki）
        from app.services.wiki import generate_article_wiki

        await generate_article_wiki(session, job["article_id"], settings)

    async def _run_generate_topic_wiki(session, job, settings, llm_client=None):
        from app.services.wiki import generate_article_wiki

        await generate_article_wiki(session, job["article_id"], settings)

    async def task_dispatcher(session: AsyncSession, job: dict, settings, llm_client=None):
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
            from app.llm.client import PermanentError

            raise PermanentError(f"未知任务类型: {task} (job {job['id']})")
        await handler(session, job, settings, client)

    worker_task = asyncio.create_task(worker_loop(settings, llm_client=generate_llm, task_handler=task_dispatcher))
    scheduler = setup_scheduler(settings, llm_client=generate_llm)
    scheduler.start()
    logger.info("APScheduler 已启动: fetch_all(%dh) / drain_queue(30s) / healthcheck(5m)", settings.ingestion.fetch_interval_hours)
    app.state.worker_task = worker_task
    app.state.scheduler = scheduler

    yield

    # shutdown
    scheduler.shutdown(wait=False)
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
    # 释放 advisory lock（池外长连接持有至进程退出，显式解锁后关闭）
    _adv = getattr(app.state, "advisory_conn", None) or advisory_conn
    if _adv is not None:
        try:
            await _adv.execute("SELECT pg_advisory_unlock(hashtext('topic_collection_worker'))")
        except Exception:
            pass
        try:
            await _adv.close()
        except Exception:
            pass
        logger.info("advisory lock 已释放")
    await dispose_engine()
    logger.info("lifespan 已退出")


def create_app() -> FastAPI:
    """创建 FastAPI 应用（供 uvicorn 使用）"""
    app = FastAPI(title="Topic Collection", lifespan=lifespan)

    # 静态资源（vendored JS/CSS）
    try:
        app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
    except RuntimeError:
        # 目录不存在时（如 CI）跳过
        pass

    # 路由（Phase 2 骨架，全部只做路由+调 service）
    from app.api import articles, dashboard, feeds, graph, health, reports, search as search_api, settings as settings_api, topics, wiki

    app.include_router(dashboard.router)
    app.include_router(health.router)
    app.include_router(settings_api.router)
    app.include_router(articles.router)
    app.include_router(feeds.router)
    app.include_router(wiki.router)
    app.include_router(search_api.router)
    app.include_router(graph.router)
    app.include_router(topics.router)
    app.include_router(reports.router)

    return app


app = create_app()
