"""FakeLLM — 内存 mock 三端点，用于开发期 + 集成测试（DESIGN §14 切片一）

固定回放 fixture 响应，不调用真实 LLM。
"""

from __future__ import annotations

import json
import logging

from app.llm.base import (
    EmbedResult,
    GenerateRequest,
    GenerateResult,
    HealthStatus,
    RerankResult,
    now_ms,
)

logger = logging.getLogger(__name__)

# ── 固定 fixture 响应 ──────────────────────────────────────────────

FIXTURE_SUMMARIZE = {
    "summary_zh": "这是一篇测试文章的摘要，用于验证端到端流程。文章讨论了主题信息聚合系统的设计与实现。",
    "key_points": [
        "系统采用单进程全异步架构",
        "使用 PostgreSQL + pgvector 存储和检索",
        "LLM 处理走本地 oMLX 服务",
    ],
    "confidence": 0.85,
}

FIXTURE_CLASSIFY_TOPICS = {"scores": {"1": 0.8}}

FIXTURE_WIKI_ENTRY = "# 测试词条\n\n这是一个测试词条内容。\n\n## 要点\n\n- 要点1\n- 要点2"

# 固定 1536 维向量（伪随机但确定性）
_FIXTURE_VECTOR = [0.01] * 1536
_FIXTURE_VECTOR[0] = 0.5
_FIXTURE_VECTOR[1] = 0.3


class FakeLLMProvider:
    """FakeLLM Provider — 三端点内存实现，固定回放。

    用法：
        provider = FakeLLMProvider()
        client = LLMClient(provider, max_concurrency=1)
    """

    def __init__(
        self,
        generation_model: str = "FakeLLM",
        embedding_model: str = "FakeLLM-Embed",
        rerank_model: str | None = "FakeLLM-Rerank",
    ):
        self.name = "fakellm"
        self.base_url = "http://fake"
        self.api_key = None
        self.generation_model = generation_model
        self.embedding_model = embedding_model
        self.rerank_model = rerank_model
        self.embed_instruct_prefix = ""  # FakeLLM 不加 instruct prefix
        self._call_count = 0
        self._generate_response: dict | str | None = None

    def set_generate_response(self, response: dict | str | None):
        """设置自定义 generate 响应（None = 使用默认 fixture）。"""
        self._generate_response = response

    async def generate(self, req: GenerateRequest) -> GenerateResult:
        """模拟 generate：返回固定 JSON fixture。"""
        self._call_count += 1
        t0 = now_ms()

        if self._generate_response is not None:
            if isinstance(self._generate_response, dict):
                text = json.dumps(self._generate_response, ensure_ascii=False)
            else:
                text = self._generate_response
        else:
            # 根据消息内容选择 fixture
            messages_str = str(req.messages).lower()
            if "摘要" in messages_str or "summary" in messages_str:
                text = json.dumps(FIXTURE_SUMMARIZE, ensure_ascii=False)
            elif "主题" in messages_str or "topic" in messages_str:
                text = json.dumps(FIXTURE_CLASSIFY_TOPICS, ensure_ascii=False)
            elif "词条" in messages_str or "wiki" in messages_str:
                text = FIXTURE_WIKI_ENTRY
            else:
                text = json.dumps(FIXTURE_SUMMARIZE, ensure_ascii=False)

        return GenerateResult(
            text=text,
            finish_reason="stop",
            usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            latency_ms=now_ms() - t0,
        )

    async def embed(
        self, texts: list[str], model: str | None = None
    ) -> EmbedResult:
        """模拟 embed：返回固定 1536 维向量。"""
        self._call_count += 1
        t0 = now_ms()
        embeddings = [_FIXTURE_VECTOR[:] for _ in texts]
        return EmbedResult(
            embeddings=embeddings,
            model=model or self.embedding_model,
            dim=1536,
            latency_ms=now_ms() - t0,
        )

    async def rerank(
        self, query: str, docs: list[str], top_n: int
    ) -> RerankResult:
        """模拟 rerank：按原始顺序返回。"""
        self._call_count += 1
        t0 = now_ms()
        n = min(top_n, len(docs))
        return RerankResult(
            indices=list(range(n)),
            scores=[0.9 - i * 0.1 for i in range(n)],
            latency_ms=now_ms() - t0,
        )

    async def healthcheck(self) -> HealthStatus:
        """模拟 healthcheck：始终健康。"""
        return HealthStatus(
            healthy=True,
            models=[self.generation_model, self.embedding_model],
            latency_ms=1,
        )

    @property
    def call_count(self) -> int:
        return self._call_count
