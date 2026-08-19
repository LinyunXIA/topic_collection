"""LLM Adapter 测试 — ProviderPatch + LLMAdapter

覆盖：patch 应用（请求侧 + 响应侧）、think 标签清理、
代码围栏清理、finish_reason 映射、endpoint 路径。
"""

from __future__ import annotations

import pytest

from app.llm.adapter import LLMAdapter, strip_think_tags, strip_code_fences
from app.llm.base import GenerateRequest
from app.llm.patches import (
    ProviderPatch,
    OMLX_PATCH,
    MINIMAX_PATCH,
    DEEPSEEK_CHAT_PATCH,
    DEEPSEEK_REASONER_PATCH,
    OPENAI_PATCH,
)


# ── strip 函数 ────────────────────────────────────────────────────

class TestStripThinkTags:
    def test_no_think_tags(self):
        assert strip_think_tags("hello world") == "hello world"

    def test_single_think_block(self):
        text = "<think>reasoning here</think>\n\nactual answer"
        assert strip_think_tags(text) == "actual answer"

    def test_multi_line_think(self):
        text = "<think>line1\nline2\nline3</think>\nresult"
        assert strip_think_tags(text) == "result"

    def test_no_content_after_think(self):
        text = "<think>only thinking</think>"
        assert strip_think_tags(text) == ""

    def test_multiple_think_blocks(self):
        text = "<think>a</think>mid<think>thought</think>end"
        assert strip_think_tags(text) == "midend"


class TestStripCodeFences:
    def test_no_fences(self):
        assert strip_code_fences('{"key": "val"}') == '{"key": "val"}'

    def test_json_fence(self):
        text = '```json\n{"key": "val"}\n```'
        assert strip_code_fences(text) == '{"key": "val"}'

    def test_plain_fence(self):
        text = '```\n{"key": "val"}\n```'
        assert strip_code_fences(text) == '{"key": "val"}'

    def test_fence_without_closing(self):
        text = '```json\n{"key": "val"}'
        assert strip_code_fences(text) == '{"key": "val"}'


# ── build_payload ──────────────────────────────────────────────────

class TestBuildGeneratePayload:
    def test_basic_payload(self):
        adapter = LLMAdapter()
        req = GenerateRequest(model="m", messages=[{"role": "user", "content": "hi"}])
        payload = adapter.build_generate_payload(req, "default-model")
        assert payload["model"] == "m"
        assert payload["messages"] == [{"role": "user", "content": "hi"}]
        assert payload["temperature"] == 0.3

    def test_default_model_fallback(self):
        adapter = LLMAdapter()
        req = GenerateRequest(model="", messages=[{"role": "user", "content": "hi"}])
        payload = adapter.build_generate_payload(req, "fallback")
        assert payload["model"] == "fallback"

    def test_json_mode(self):
        adapter = LLMAdapter()
        req = GenerateRequest(model="m", messages=[], json_mode=True)
        payload = adapter.build_generate_payload(req, "m")
        assert payload["response_format"] == {"type": "json_object"}

    def test_max_tokens(self):
        adapter = LLMAdapter()
        req = GenerateRequest(model="m", messages=[], max_tokens=500)
        payload = adapter.build_generate_payload(req, "m")
        assert payload["max_tokens"] == 500

    def test_drop_fields(self):
        patch = ProviderPatch(drop_request_fields=["temperature"])
        adapter = LLMAdapter(patch)
        req = GenerateRequest(model="m", messages=[], temperature=0.7)
        payload = adapter.build_generate_payload(req, "m")
        assert "temperature" not in payload

    def test_extra_fields(self):
        patch = ProviderPatch(extra_body_fields={"top_p": 0.9})
        adapter = LLMAdapter(patch)
        req = GenerateRequest(model="m", messages=[])
        payload = adapter.build_generate_payload(req, "m")
        assert payload["top_p"] == 0.9


class TestBuildEmbedPayload:
    def test_no_dimensions(self):
        adapter = LLMAdapter()
        payload = adapter.build_embed_payload(["hello"], None, "model-a")
        assert payload["model"] == "model-a"
        assert "dimensions" not in payload

    def test_with_dimensions(self):
        adapter = LLMAdapter(OMLX_PATCH)
        payload = adapter.build_embed_payload(["hello"], None, "model-a")
        assert payload["dimensions"] == 1536

    def test_model_override(self):
        adapter = LLMAdapter()
        payload = adapter.build_embed_payload(["hello"], "custom-model", "default")
        assert payload["model"] == "custom-model"


# ── parse_response ─────────────────────────────────────────────────

class TestParseGenerateResponse:
    def test_basic_response(self):
        adapter = LLMAdapter()
        data = {
            "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10},
        }
        result = adapter.parse_generate_response(data, "m")
        assert result.text == "hello"
        assert result.finish_reason == "stop"
        assert result.usage == {"prompt_tokens": 10}

    def test_think_tag_stripped(self):
        adapter = LLMAdapter(MINIMAX_PATCH)
        data = {
            "choices": [{"message": {"content": "<think>reasoning</think>\nanswer"}, "finish_reason": "stop"}],
        }
        result = adapter.parse_generate_response(data, "m")
        assert result.text == "answer"
        assert "<think>" not in result.text

    def test_code_fence_stripped(self):
        adapter = LLMAdapter(MINIMAX_PATCH)
        data = {
            "choices": [{"message": {"content": "<think>think</think>\n```json\n{\"k\": \"v\"}\n```"}, "finish_reason": "stop"}],
        }
        result = adapter.parse_generate_response(data, "m")
        assert result.text == '{"k": "v"}'

    def test_finish_reason_mapped(self):
        patch = ProviderPatch(finish_reason_map={"length": "stop"})
        adapter = LLMAdapter(patch)
        data = {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "length"}],
        }
        result = adapter.parse_generate_response(data, "m")
        assert result.finish_reason == "stop"

    def test_finish_reason_not_mapped(self):
        adapter = LLMAdapter()
        data = {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "length"}],
        }
        result = adapter.parse_generate_response(data, "m")
        assert result.finish_reason == "length"


class TestParseEmbedResponse:
    def test_basic(self):
        adapter = LLMAdapter()
        data = {
            "data": [{"index": 0, "embedding": [0.1, 0.2]}, {"index": 1, "embedding": [0.3, 0.4]}],
            "model": "m",
        }
        result = adapter.parse_embed_response(data, "default")
        assert len(result.embeddings) == 2
        assert result.dim == 2
        assert result.model == "m"

    def test_sorted_by_index(self):
        adapter = LLMAdapter()
        data = {
            "data": [{"index": 1, "embedding": [0.3]}, {"index": 0, "embedding": [0.1]}],
            "model": "m",
        }
        result = adapter.parse_embed_response(data, "default")
        assert result.embeddings == [[0.1], [0.3]]


# ── URL 路径 ───────────────────────────────────────────────────────

class TestAdapterUrls:
    def test_default_paths(self):
        adapter = LLMAdapter()
        assert adapter.chat_url("http://localhost:8000") == "http://localhost:8000/v1/chat/completions"
        assert adapter.embed_url("http://localhost:8000") == "http://localhost:8000/v1/embeddings"
        assert adapter.models_url("http://localhost:8000") == "http://localhost:8000/v1/models"

    def test_deepseek_paths(self):
        adapter = LLMAdapter(DEEPSEEK_CHAT_PATCH)
        assert adapter.chat_url("https://api.deepseek.com") == "https://api.deepseek.com/chat/completions"
        assert adapter.embed_url("https://api.deepseek.com") == "https://api.deepseek.com/v1/embeddings"
        assert adapter.models_url("https://api.deepseek.com") == "https://api.deepseek.com/v1/models"


# ── 预定义 Patch ───────────────────────────────────────────────────

class TestPredefinedPatches:
    def test_omlx_patch(self):
        assert OMLX_PATCH.send_dimensions is True
        assert OMLX_PATCH.dimensions_value == 1536
        assert OMLX_PATCH.strip_think_tags is False

    def test_minimax_patch(self):
        assert MINIMAX_PATCH.strip_think_tags is True
        assert MINIMAX_PATCH.strip_code_fences is True
        assert MINIMAX_PATCH.send_dimensions is False

    def test_deepseek_chat_patch(self):
        assert DEEPSEEK_CHAT_PATCH.chat_path == "/chat/completions"
        assert DEEPSEEK_CHAT_PATCH.strip_think_tags is False

    def test_deepseek_reasoner_patch(self):
        assert DEEPSEEK_REASONER_PATCH.strip_think_tags is True
        assert DEEPSEEK_REASONER_PATCH.chat_path == "/chat/completions"
        assert "temperature" in DEEPSEEK_REASONER_PATCH.drop_request_fields

    def test_openai_patch(self):
        assert OPENAI_PATCH.strip_think_tags is False
        assert OPENAI_PATCH.send_dimensions is False
        assert OPENAI_PATCH.chat_path == "/v1/chat/completions"
