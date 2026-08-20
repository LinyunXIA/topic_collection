"""健康检查 — Phase 2（DESIGN §14 2.1.2）

GET /api/health  LLM 队列 worker 状态
GET /api/llm-status  LLM ping（供 htmx 每 30s 轮询，复用同逻辑）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_llm_client, get_session, get_settings
from app.config import Settings
from app.llm.client import LLMClient

router = APIRouter()


@router.get("/api/health")
async def health(
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    llm_client: LLMClient | None = Depends(get_llm_client),
):
    """LLM 队列 worker 状态 + LLM 健康，HTMX 每 30s 轮询外层横幅（DB 失败时降级）"""
    # 队列深度（降级）
    try:
        result = await session.execute(text("SELECT status, COUNT(*) FROM processing_jobs GROUP BY status"))
        queue = {row[0]: row[1] for row in result.fetchall()}
    except Exception:
        queue = {}
    # LLM 健康
    llm_healthy = None
    llm_error = None
    latency_ms = None
    if llm_client:
        try:
            status = await llm_client.healthcheck()
            llm_healthy = status.healthy
            llm_error = status.error
            latency_ms = status.latency_ms
        except Exception as e:
            llm_healthy = False
            llm_error = str(e)
    else:
        # 短命进程：即时探测
        try:
            from app.llm.factory import build_provider
            from app.llm.client import LLMClient as LC

            provider = build_provider("generate", settings)
            tmp = LC(provider)
            st = await tmp.healthcheck()
            llm_healthy = st.healthy
            llm_error = st.error
            latency_ms = st.latency_ms
        except Exception as e:
            llm_healthy = False
            llm_error = str(e)

    return {
        "llm_healthy": llm_healthy,
        "llm_error": llm_error,
        "latency_ms": latency_ms,
        "queue_depth": queue,
        "last_healthcheck_at": None,
    }


@router.get("/api/llm-status")
async def llm_status(
    request: Request,
    settings: Settings = Depends(get_settings),
    llm_client: LLMClient | None = Depends(get_llm_client),
):
    """LLM ping（htmx 横幅每 30s）"""
    # 复用 health 逻辑，返回 llm 段
    if llm_client:
        try:
            st = await llm_client.healthcheck()
            return {"healthy": st.healthy, "error": st.error, "latency_ms": st.latency_ms, "models": st.models[:3] if st.models else []}
        except Exception as e:
            return {"healthy": False, "error": str(e), "latency_ms": None, "models": []}
    try:
        from app.llm.factory import build_provider
        from app.llm.client import LLMClient as LC

        provider = build_provider("generate", settings)
        tmp = LC(provider)
        st = await tmp.healthcheck()
        return {"healthy": st.healthy, "error": st.error, "latency_ms": st.latency_ms, "models": st.models[:3] if st.models else []}
    except Exception as e:
        return {"healthy": False, "error": str(e), "latency_ms": None, "models": []}
