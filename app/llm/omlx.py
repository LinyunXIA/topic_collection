"""oMLX LLM Provider — HTTP 传输层（本地推理）

请求/响应格式委托给 LLMAdapter（80% 通用逻辑 + OMLX_PATCH）。
三端点：/v1/chat/completions、/v1/embeddings、/v1/rerank。
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
from app.llm.patches import OMLX_PATCH, ProviderPatch

logger = logging.getLogger(__name__)

# 保留模块级别名，供旧 import 路径兼容
_EMBED_INSTRUCT_PREFIX = (
    "Given a web search query, retrieve relevant passages that answer the query: "
)


class OMLXProvider:
    """oMLX OpenAI 兼容 Provider — 只做 HTTP 传输。"""

    embed_instruct_prefix: str = _EMBED_INSTRUCT_PREFIX

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        api_key: str | None = None,
        generation_model: str = "Qwen3.8-27B-MLX-4bit",
        embedding_model: str = "Qwen3-Embedding-8B-4bit-DWQ",
        rerank_model: str | None = "Qwen3-Reranker-4B-mxfp8",
        patch: ProviderPatch | None = None,
    ):
        self.name = "omlx"
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or None
        self.generation_model = generation_model
        self.embedding_model = embedding_model
        self.rerank_model = rerank_model
        self._adapter = LLMAdapter(patch or OMLX_PATCH)

    def _headers(self) -> dict[str, str]:
        """构建请求头：本机不鉴权时不带 Authorization（DESIGN §4.2）。"""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _post(
        self, url: str, payload: dict, timeout: float = 180
    ) -> dict[str, Any]:
        """发送 POST 请求并返回 JSON 响应。

        走共享出口 egress.safe_post：本地 oMLX(localhost/私网) 视为非外发放行，
        外部域名须命中白名单（PRD §12，#78）。
        """
        resp = await safe_post(url, headers=self._headers(), json=payload, timeout=timeout)
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
        """POST /v1/rerank（Cohere 风格，DESIGN §4.2）"""
        if not self.rerank_model:
            return RerankResult(indices=list(range(min(top_n, len(docs)))), scores=[])

        payload = {
            "model": self.rerank_model,
            "query": query,
            "documents": docs,
            "top_n": top_n,
        }

        t0 = now_ms()
        data = await self._post(
            f"{self.base_url}/v1/rerank", payload, timeout=120
        )
        latency_ms = now_ms() - t0

        results = sorted(data.get("results", []), key=lambda x: x["index"])
        return RerankResult(
            indices=[r["index"] for r in results],
            scores=[r["relevance_score"] for r in results],
            latency_ms=latency_ms,
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
