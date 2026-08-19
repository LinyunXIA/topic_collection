"""LLM Adapter — 统一适配层（80% 通用 OpenAI 逻辑）

请求构建 + 响应解析，通过 ProviderPatch 覆盖 20% 差异。
Provider（OpenAI/OMLX）只做 HTTP 传输，请求/响应格式委托给本模块。

架构：
    GenerateRequest (内部DTO)
        ↓ build_generate_payload / build_embed_payload
    OpenAI 标准 payload + provider patch
        ↓ HTTP POST
    Raw JSON Response
        ↓ parse_generate_response / parse_embed_response
    GenerateResult / EmbedResult (内部DTO)
"""

from __future__ import annotations

import re
from typing import Any

from app.llm.base import (
    EmbedResult,
    GenerateRequest,
    GenerateResult,
    now_ms,
)
from app.llm.patches import ProviderPatch


class LLMAdapter:
    """OpenAI 兼容协议的统一适配层。"""

    def __init__(self, patch: ProviderPatch | None = None):
        self.patch = patch or ProviderPatch()

    # ── Generate ──────────────────────────────────────────────────

    def build_generate_payload(
        self, req: GenerateRequest, default_model: str
    ) -> dict[str, Any]:
        """构建 OpenAI 标准 chat/completions payload。"""
        payload: dict[str, Any] = {
            "model": req.model or default_model,
            "messages": req.messages,
            "temperature": req.temperature,
        }
        if req.max_tokens is not None:
            payload["max_tokens"] = req.max_tokens
        if req.json_mode:
            payload["response_format"] = {"type": "json_object"}

        # 应用 patch：移除不支持的字段
        for field_name in self.patch.drop_request_fields:
            payload.pop(field_name, None)

        # 应用 patch：追加额外字段
        payload.update(self.patch.extra_body_fields)

        return payload

    def parse_generate_response(
        self, data: dict[str, Any], default_model: str
    ) -> GenerateResult:
        """解析 OpenAI 标准 chat/completions 响应。"""
        choice = data["choices"][0]
        text = choice["message"]["content"]

        # 应用响应 patch
        if self.patch.strip_think_tags:
            text = strip_think_tags(text)
        if self.patch.strip_code_fences:
            text = strip_code_fences(text)

        finish_reason = choice.get("finish_reason", "")
        if finish_reason in self.patch.finish_reason_map:
            finish_reason = self.patch.finish_reason_map[finish_reason]

        usage = data.get("usage")

        return GenerateResult(
            text=text.strip(),
            finish_reason=finish_reason,
            usage=usage,
        )

    # ── Embed ─────────────────────────────────────────────────────

    def build_embed_payload(
        self, texts: list[str], model: str | None, default_model: str
    ) -> dict[str, Any]:
        """构建 OpenAI 标准 embeddings payload。"""
        payload: dict[str, Any] = {
            "model": model or default_model,
            "input": texts,
        }
        if self.patch.send_dimensions:
            payload["dimensions"] = self.patch.dimensions_value
        return payload

    def parse_embed_response(
        self, data: dict[str, Any], default_model: str
    ) -> EmbedResult:
        """解析 OpenAI 标准 embeddings 响应。"""
        sorted_data = sorted(data["data"], key=lambda x: x["index"])
        embeddings = [item["embedding"] for item in sorted_data]
        dim = len(embeddings[0]) if embeddings else 0

        return EmbedResult(
            embeddings=embeddings,
            model=data.get("model", default_model),
            dim=dim,
        )

    # ── URL 辅助 ──────────────────────────────────────────────────

    def chat_url(self, base_url: str) -> str:
        return f"{base_url}{self.patch.chat_path}"

    def embed_url(self, base_url: str) -> str:
        return f"{base_url}{self.patch.embed_path}"

    def models_url(self, base_url: str) -> str:
        return f"{base_url}{self.patch.models_path}"


# ── 内部工具函数 ──────────────────────────────────────────────────

def strip_think_tags(text: str) -> str:
    """清理 <think>...</think> 块（思考模型内联 think 内容）。"""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


def strip_code_fences(text: str) -> str:
    """清理 ```json ... ``` 代码围栏。"""
    text = re.sub(r"```(?:json)?\s*\n?", "", text)
    text = re.sub(r"```\s*$", "", text)
    return text.strip()
