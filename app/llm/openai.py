"""OpenAI 兼容 LLM Provider 实现 — 外部 API

支持 OpenAI / DeepSeek / Moonshot / DashScope (OpenAI-mode) / 智谱 / vLLM 等
OpenAI 兼容协议的外部 API。Embed/rerank 不走外部（隐私，强制本地）。
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


class OpenAIProvider:
    """OpenAI 兼容 Provider — 外部 API。"""

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        generation_model: str = "gpt-4o-mini",
        embedding_model: str = "text-embedding-3-small",
        rerank_model: str | None = None,  # OpenAI 不支持 rerank
    ):
        self.name = "openai"
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or None
        self.generation_model = generation_model
        self.embedding_model = embedding_model
        self.rerank_model = rerank_model
        # OpenAI embedding 不需要 instruct prefix
        self.embed_instruct_prefix: str = ""

    def _headers(self) -> dict[str, str]:
        """构建请求头：外部 API 必须鉴权。"""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    @staticmethod
    def _strip_think_tags(text: str) -> str:
        """清理思考模型的 <think>...</think> 标签和 ```json 代码围栏。

        MiniMax-M3 等思考模型即使开启 json_mode，响应仍可能包含
        think 块和代码围栏。返回纯 JSON 文本供下游 parse_with_repair 处理。
        """
        import re
        # 去除 <think>...</think> 块
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
        # 去除 ```json ... ``` 代码围栏
        text = re.sub(r"```(?:json)?\s*\n?", "", text)
        text = re.sub(r"```\s*$", "", text)
        return text.strip()

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
        text = self._strip_think_tags(choice["message"]["content"])
        return GenerateResult(
            text=text,
            finish_reason=choice.get("finish_reason", ""),
            usage=data.get("usage"),
            latency_ms=latency_ms,
        )

    async def embed(
        self, texts: list[str], model: str | None = None
    ) -> EmbedResult:
        """POST /v1/embeddings

        不传 dimensions 参数（不同 OpenAI 兼容 API 支持情况不一），
        由下游 complete_embed 钩子统一校验维度。
        """
        payload = {
            "model": model or self.embedding_model,
            "input": texts,
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
        """OpenAI 兼容 API 不支持 /v1/rerank（Cohere 风格）。

        Rerank 必须走本地 OMLXProvider（factory 会跳过 OpenAI），
        此方法不应被调用。
        """
        raise NotImplementedError(
            "OpenAI 兼容 provider 不支持 rerank；"
            "请使用本地 OMLXProvider（factory 会自动选择）"
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
