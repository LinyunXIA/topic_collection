"""LLMClient 门面 — DESIGN §4.4

并发信号量 + 指数退避重试 + 健康标志。
重试/超时只在此层处理，services 不碰传输。
错误分类：401/403/400 → 永久（不退避）；5xx/超时/连接拒绝 → 瞬时（退避）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.llm.base import (
    EmbedResult,
    GenerateRequest,
    GenerateResult,
    HealthStatus,
    LLMProvider,
    RerankResult,
)

logger = logging.getLogger(__name__)

# 永久错误的 HTTP 状态码（不退避，直接抛）
# 瞬时错误（5xx/超时/连接拒绝）由 _retry_transient 内联走退避重试，不再单独分类
_PERMANENT_STATUS_CODES = {400, 401, 403}


class PermanentError(Exception):
    """永久/配置错误（401/403/400），不走指数退避。"""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class LLMClient:
    """LLM 门面：并发控制 + 重试 + 健康标志。

    - 并发信号量（默认 1，DESIGN §4.4 待验证假设）
    - 指数退避重试（仅瞬时错误：5xx/超时/连接拒绝）
    - 401/403/400 → PermanentError（不退避，由 job 层按 max_attempts 死信）
    - healthy 标志：进程内内存状态，Phase 1 单进程下全局可见
    """

    def __init__(
        self,
        provider: LLMProvider,
        max_concurrency: int = 1,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
    ):
        self.provider = provider
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._max_retries = max_retries
        self._base_delay = base_delay
        self._max_delay = max_delay
        self.healthy: bool = False
        self._consecutive_failures: int = 0

    async def _retry_transient(self, coro_factory, operation: str = "llm_call"):
        """对瞬时错误执行指数退避重试。401/403/400 为永久错误，不重试。

        注意：except 块内调用 raise 的方法会导致异常绕过同 try 的其他 except，
        因此 HTTP 状态码分类在 httpx.HTTPStatusError 分支内直接内联判断。
        """
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                result = await coro_factory()
                self._consecutive_failures = 0
                return result
            except PermanentError:
                raise  # 永久错误不重试
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                # 内联分类：401/403/400 → 永久错误，不重试
                if status in _PERMANENT_STATUS_CODES:
                    raise PermanentError(
                        f"永久错误 {status}: {e}", status
                    ) from e
                # 其他（5xx/429/其它）→ 瞬时，走退避重试
                last_exc = e
                self._consecutive_failures += 1
                if attempt < self._max_retries:
                    delay = min(self._base_delay * (2**attempt), self._max_delay)
                    logger.warning(
                        "%s 第 %d 次失败 (HTTP %d)，%.1fs 后重试",
                        operation, attempt + 1, status, delay,
                    )
                    await asyncio.sleep(delay)
            except Exception as e:
                last_exc = e
                self._consecutive_failures += 1
                if attempt < self._max_retries:
                    delay = min(self._base_delay * (2**attempt), self._max_delay)
                    logger.warning(
                        "%s 第 %d 次失败: %s，%.1fs 后重试",
                        operation, attempt + 1, e, delay,
                    )
                    await asyncio.sleep(delay)
        raise last_exc  # type: ignore[misc]

    async def generate(self, req: GenerateRequest) -> GenerateResult:
        """生成（带并发控制 + 重试）。"""
        async with self._semaphore:
            return await self._retry_transient(
                lambda: self.provider.generate(req),
                operation=f"generate({req.model})",
            )

    async def embed(
        self, texts: list[str], model: str | None = None
    ) -> EmbedResult:
        """嵌入（带并发控制 + 重试）。"""
        async with self._semaphore:
            return await self._retry_transient(
                lambda: self.provider.embed(texts, model),
                operation=f"embed(model={model})",
            )

    async def embed_query(self, text: str) -> EmbedResult:
        """嵌入查询文本（自动加 instruct prefix，DESIGN §4.2）。"""
        prefix = getattr(self.provider, "embed_instruct_prefix", "")
        prefixed = prefix + text
        return await self.embed([prefixed])

    async def embed_documents(self, texts: list[str]) -> EmbedResult:
        """嵌入文档文本（不加 instruct prefix，DESIGN §4.2）。"""
        return await self.embed(texts)

    async def rerank(
        self, query: str, docs: list[str], top_n: int
    ) -> RerankResult:
        """重排（带并发控制 + 重试）。"""
        async with self._semaphore:
            return await self._retry_transient(
                lambda: self.provider.rerank(query, docs, top_n),
                operation="rerank",
            )

    async def healthcheck(self) -> HealthStatus:
        """健康探测（不走信号量，不重试）。"""
        status = await self.provider.healthcheck()
        self.healthy = status.healthy
        return status

    @property
    def is_healthy(self) -> bool:
        """当前健康状态（供 worker 门控）。"""
        return self.healthy
