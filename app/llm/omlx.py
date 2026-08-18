"""oMLX LLM Provider 实现 — DESIGN §4.2

OpenAI 兼容 REST API，本机不鉴权（已实测确认）。
三端点：/v1/chat/completions、/v1/embeddings、/v1/rerank。
"""

from __future__ import annotations

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
    now_ms,
)

logger = logging.getLogger(__name__)

# Qwen3-Embedding instruct prefix（DESIGN §4.2）
EMBED_INSTRUCT_PREFIX = (
    "Given a web search query, retrieve relevant passages that answer the query: "
)


class OMLXProvider:
    """oMLX OpenAI 兼容 Provider。"""

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        api_key: str | None = None,
        generation_model: str = "Qwen3.8-27B-MLX-4bit",
        embedding_model: str = "Qwen3-Embedding-8B-4bit-DWQ",
        rerank_model: str | None = "Qwen3-Reranker-4B-mxfp8",
    ):
        self.name = "omlx"
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or None
        self.generation_model = generation_model
        self.embedding_model = embedding_model
        self.rerank_model = rerank_model

    def _headers(self) -> dict[str, str]:
        """构建请求头：本机不鉴权时不带 Authorization（DESIGN §4.2）。"""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def generate(self, req: GenerateRequest) -> GenerateResult:
        """POST /v1/chat/completions"""
        payload: dict[str, Any] = {
            "model": req.model or self.generation_model,
            "messages": req.messages,
            "temperature": req.temperature,
        }
        if req.max_tokens is not None:
            payload["max_tokens"] = req.max_tokens
        if req.json_mode:
            payload["response_format"] = {"type": "json_object"}

        t0 = now_ms()
        async with httpx.AsyncClient(timeout=req.timeout_s) as client:
            resp = await client.post(
                f"{self.base_url}/v1/chat/completions",
                headers=self._headers(),
                json=payload,
            )
            resp.raise_for_status()

        data = resp.json()
        latency_ms = now_ms() - t0
        choice = data["choices"][0]
        return GenerateResult(
            text=choice["message"]["content"],
            finish_reason=choice.get("finish_reason", ""),
            usage=data.get("usage"),
            latency_ms=latency_ms,
        )

    async def embed(
        self, texts: list[str], model: str | None = None
    ) -> EmbedResult:
        """POST /v1/embeddings

        指令感知（DESIGN §4.2）：调用方通过 embed_query/embed_documents 区分，
        此处不做 prefix 处理——prefix 由 client 封装层统一加。
        """
        payload = {
            "model": model or self.embedding_model,
            "input": texts,
            "dimensions": 1536,  # DESIGN §5.2: MRL 截断至 1536
        }

        t0 = now_ms()
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{self.base_url}/v1/embeddings",
                headers=self._headers(),
                json=payload,
            )
            resp.raise_for_status()

        data = resp.json()
        latency_ms = now_ms() - t0

        # 按 index 排序确保顺序一致
        sorted_data = sorted(data["data"], key=lambda x: x["index"])
        embeddings = [item["embedding"] for item in sorted_data]
        dim = len(embeddings[0]) if embeddings else 0

        return EmbedResult(
            embeddings=embeddings,
            model=data.get("model", payload["model"]),
            dim=dim,
            latency_ms=latency_ms,
        )

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
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{self.base_url}/v1/rerank",
                headers=self._headers(),
                json=payload,
            )
            resp.raise_for_status()

        data = resp.json()
        latency_ms = now_ms() - t0

        results = sorted(data.get("results", []), key=lambda x: x["index"])
        return RerankResult(
            indices=[r["index"] for r in results],
            scores=[r["relevance_score"] for r in results],
            latency_ms=latency_ms,
        )

    async def healthcheck(self) -> HealthStatus:
        """GET /v1/models 探测端点可用性。"""
        t0 = now_ms()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{self.base_url}/v1/models",
                    headers=self._headers(),
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
