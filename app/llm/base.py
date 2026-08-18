"""LLM Provider 基础抽象 — DESIGN §4.1

Protocol + 请求/结果类型。Provider 实现三能力：generate / embed / rerank。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class GenerateRequest:
    """生成请求。"""
    model: str
    messages: list[dict]
    temperature: float = 0.3
    max_tokens: int | None = None
    json_mode: bool = False
    timeout_s: float = 180


@dataclass
class GenerateResult:
    """生成结果。"""
    text: str
    finish_reason: str
    usage: dict | None = None
    latency_ms: int = 0


@dataclass
class EmbedResult:
    """嵌入结果。"""
    embeddings: list[list[float]]
    model: str
    dim: int
    latency_ms: int = 0


@dataclass
class RerankResult:
    """重排结果。"""
    indices: list[int]
    scores: list[float]
    latency_ms: int = 0


@dataclass
class HealthStatus:
    """LLM 健康状态。"""
    healthy: bool = False
    models: list[str] = field(default_factory=list)
    error: str | None = None
    latency_ms: int = 0


@runtime_checkable
class LLMProvider(Protocol):
    """LLM Provider 协议 — 三能力 + 健康探测。"""

    name: str
    base_url: str
    api_key: str | None
    generation_model: str
    embedding_model: str
    rerank_model: str | None

    async def generate(self, req: GenerateRequest) -> GenerateResult:
        """POST /v1/chat/completions"""
        ...

    async def embed(self, texts: list[str], model: str | None = None) -> EmbedResult:
        """POST /v1/embeddings"""
        ...

    async def rerank(self, query: str, docs: list[str], top_n: int) -> RerankResult:
        """POST /v1/rerank（Cohere 风格）"""
        ...

    async def healthcheck(self) -> HealthStatus:
        """GET /v1/models 或逐端点探测"""
        ...


def now_ms() -> int:
    """当前时间戳（毫秒）。"""
    return int(time.time() * 1000)
