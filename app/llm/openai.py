"""OpenAI 兼容 LLM Provider — HTTP 传输层

请求/响应格式委托给 LLMAdapter（80% 通用逻辑 + 20% patch）。
支持 OpenAI / MiniMax / DeepSeek / Moonshot / DashScope 等。
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.egress import safe_get, safe_post
from app.llm.adapter import LLMAdapter
from app.llm.base import (
    EmbedResult,
    GenerateRequest,
    GenerateResult,
    HealthStatus,
    LLMProvider,
    RerankResult,
    now_ms,
)
from app.llm.patches import ProviderPatch

logger = logging.getLogger(__name__)


class OpenAIProvider:
    """OpenAI 兼容 Provider — 只做 HTTP 传输。"""

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        generation_model: str = "gpt-4o-mini",
        embedding_model: str = "text-embedding-3-small",
        rerank_model: str | None = None,
        patch: ProviderPatch | None = None,
    ):
        self.name = "openai"
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or None
        self.generation_model = generation_model
        self.embedding_model = embedding_model
        self.rerank_model = rerank_model
        self.embed_instruct_prefix: str = ""
        self._adapter = LLMAdapter(patch or ProviderPatch())

    def _headers(self) -> dict[str, str]:
        """构建请求头：外部 API 必须鉴权。"""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _post(
        self, url: str, payload: dict, timeout: float = 180
    ) -> dict[str, Any]:
        """发送 POST 请求并返回 JSON 响应。

        走共享出口 egress.safe_post：外部 LLM 域名必须命中白名单（PRD §12），
        未命中直接抛 PermanentError，避免文章全文发往任意域名（#78）。
        """
        resp = await safe_post(url, headers=self._headers(), json=payload, timeout=timeout)
        if resp.status_code != 200:
            logger.error(
                "API %s %s → %d: %s",
                resp.request.method, url, resp.status_code, resp.text[:500],
            )
        resp.raise_for_status()
        return resp.json()

    async def generate(self, req: GenerateRequest) -> GenerateResult:
        """POST /v1/chat/completions"""
        payload = self._adapter.build_generate_payload(req, self.generation_model)
        t0 = now_ms()
        data = await self._post(self._adapter.chat_url(self.base_url), payload, req.timeout_s)
        result = self._adapter.parse_generate_response(data, self.generation_model)
        result.latency_ms = now_ms() - t0
        return result

    async def embed(
        self, texts: list[str], model: str | None = None
    ) -> EmbedResult:
        """POST /v1/embeddings"""
        payload = self._adapter.build_embed_payload(texts, model, self.embedding_model)
        t0 = now_ms()
        data = await self._post(self._adapter.embed_url(self.base_url), payload, timeout=120)
        result = self._adapter.parse_embed_response(data, self.embedding_model)
        result.latency_ms = now_ms() - t0
        return result

    async def rerank(
        self, query: str, docs: list[str], top_n: int
    ) -> RerankResult:
        """OpenAI 兼容 API 不支持 /v1/rerank。"""
        raise NotImplementedError(
            "OpenAI 兼容 provider 不支持 rerank；请使用本地 OMLXProvider"
        )

    async def healthcheck(self) -> HealthStatus:
        """GET /v1/models 探测端点可用性（走 egress.safe_get 白名单校验）。"""
        t0 = now_ms()
        try:
            resp = await safe_get(
                self._adapter.models_url(self.base_url),
                headers=self._headers(),
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            models = [m["id"] for m in data.get("data", [])]
            return HealthStatus(
                healthy=True,
                models=models,
                latency_ms=now_ms() - t0,
            )
        except Exception as e:
            return HealthStatus(
                healthy=False,
                error=str(e),
                latency_ms=now_ms() - t0,
            )
