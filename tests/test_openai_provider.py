"""OpenAI 兼容 Provider 单元测试

覆盖：generate/embed/healthcheck 端点调用、
Authorization header、_classify_http_error 分类（Phase 0）。
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.llm.base import GenerateRequest, HealthStatus
from app.llm.client import LLMClient, PermanentError
from app.llm.factory import build_provider, _resolve_api_key
from app.config import load_settings


# ── OpenAIProvider 基本行为 ────────────────────────────────────────

class TestOpenAIProvider:
    """OpenAI provider 端到端调用测试（mock HTTP）。"""

    def _make_provider(self, api_key: str | None = "sk-test"):
        from app.llm.openai import OpenAIProvider
        return OpenAIProvider(
            base_url="https://api.openai.com/v1",
            api_key=api_key,
            generation_model="gpt-4o-mini",
            embedding_model="text-embedding-3-small",
        )

    @pytest.mark.asyncio
    async def test_generate_calls_chat_completions(self):
        """generate → POST /v1/chat/completions，Authorization header 正确。"""
        provider = self._make_provider()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "测试响应"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp) as mock_post:
            req = GenerateRequest(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "hello"}],
                json_mode=True,
            )
            result = await provider.generate(req)

        # 验证 URL 和 header
        call_args = mock_post.call_args
        assert "/v1/chat/completions" in call_args[0][0]
        assert call_args[1]["headers"]["Authorization"] == "Bearer sk-test"
        # 验证 json_mode
        payload = call_args[1]["json"]
        assert payload["response_format"] == {"type": "json_object"}
        # 验证结果
        assert result.text == "测试响应"

    @pytest.mark.asyncio
    async def test_generate_no_api_key_no_auth_header(self):
        """api_key=None 时不带 Authorization header。"""
        provider = self._make_provider(api_key=None)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp) as mock_post:
            req = GenerateRequest(model="m", messages=[{"role": "user", "content": "hi"}])
            await provider.generate(req)

        headers = mock_post.call_args[1]["headers"]
        assert "Authorization" not in headers

    @pytest.mark.asyncio
    async def test_embed_calls_embeddings_endpoint(self):
        """embed → POST /v1/embeddings，不传 dimensions 参数。"""
        provider = self._make_provider()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": [{"index": 0, "embedding": [0.1] * 1536}],
            "model": "text-embedding-3-small",
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp) as mock_post:
            result = await provider.embed(["hello world"])

        payload = mock_post.call_args[1]["json"]
        assert "/v1/embeddings" in mock_post.call_args[0][0]
        assert "dimensions" not in payload  # 不传 dimensions
        assert result.dim == 1536

    @pytest.mark.asyncio
    async def test_rerank_raises_not_implemented(self):
        """OpenAI 不支持 rerank → NotImplementedError。"""
        provider = self._make_provider()
        with pytest.raises(NotImplementedError, match="不支持 rerank"):
            await provider.rerank("query", ["doc1"], top_n=1)

    @pytest.mark.asyncio
    async def test_healthcheck_ok(self):
        """healthcheck → GET /v1/models → healthy。"""
        provider = self._make_provider()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "data": [{"id": "gpt-4o-mini"}, {"id": "text-embedding-3-small"}],
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, return_value=mock_resp):
            status = await provider.healthcheck()

        assert status.healthy is True
        assert "gpt-4o-mini" in status.models

    @pytest.mark.asyncio
    async def test_healthcheck_error(self):
        """healthcheck 失败 → unhealthy。"""
        provider = self._make_provider()

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock, side_effect=Exception("connection refused")):
            status = await provider.healthcheck()

        assert status.healthy is False
        assert "connection refused" in status.error


# ── LLMClient 401 → PermanentError（Phase 0 集成验证） ─────────────

class TestLLMClientHttpClassification:
    """验证 LLMClient._retry_transient 正确将 401 HTTP 错误转为 PermanentError。"""

    @pytest.mark.asyncio
    async def test_401_raises_permanent_error(self):
        """401 → PermanentError，不走重试。"""
        mock_provider = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "401 Unauthorized",
            request=MagicMock(),
            response=mock_resp,
        )
        mock_provider.generate.side_effect = httpx.HTTPStatusError(
            "401 Unauthorized",
            request=MagicMock(),
            response=mock_resp,
        )
        client = LLMClient(mock_provider, max_concurrency=1, max_retries=3)

        req = GenerateRequest(model="m", messages=[{"role": "user", "content": "hi"}])
        with pytest.raises(PermanentError, match="401"):
            await client.generate(req)

        # 401 是永久错误，不应该重试（只调用一次）
        assert mock_provider.generate.call_count == 1

    @pytest.mark.asyncio
    async def test_500_retries_before_failing(self):
        """500 → 瞬时错误 → _retry_transient 退避重试 max_retries 次后抛出原异常。"""
        mock_provider = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        http_err = httpx.HTTPStatusError(
            "500 Server Error",
            request=MagicMock(),
            response=mock_resp,
        )
        mock_provider.generate.side_effect = http_err
        # 短退避（0.01s）避免测试慢
        client = LLMClient(mock_provider, max_concurrency=1, max_retries=2, base_delay=0.01)

        req = GenerateRequest(model="m", messages=[{"role": "user", "content": "hi"}])
        with pytest.raises(httpx.HTTPStatusError):
            await client.generate(req)

        # 500 是瞬时错误，应重试 max_retries+1 次（1 initial + 2 retries = 3）
        assert mock_provider.generate.call_count == 3


# ── factory 构建验证 ───────────────────────────────────────────────

class TestBuildProvider:
    """验证 build_provider 正确构建 provider 并处理 API key。"""

    def _make_omlx_settings(self):
        """构造 omlx 默认 settings（不依赖 config.yaml 内容）。"""
        from app.config import LLMSettings, EmbedSettings, RerankSettings, Settings
        from unittest.mock import MagicMock
        # 用真实 pydantic 对象，避免 MagicMock 嵌套属性问题
        settings = MagicMock(spec=Settings)
        llm = LLMSettings(
            backend="omlx",
            endpoint="http://localhost:8000",
            model="Qwen3.8-27B-MLX-4bit",
            embed=EmbedSettings(backend="omlx", endpoint="http://localhost:8000"),
            rerank=RerankSettings(backend="omlx", endpoint="http://localhost:8000"),
        )
        settings.llm = llm
        return settings

    def test_build_generate_omlx(self):
        settings = self._make_omlx_settings()
        p = build_provider("generate", settings)
        assert p.name == "omlx"
        assert "8000" in p.base_url

    def test_build_embed_omlx(self):
        settings = self._make_omlx_settings()
        p = build_provider("embed", settings)
        assert p.name == "omlx"

    def test_build_rerank_omlx(self):
        settings = self._make_omlx_settings()
        p = build_provider("rerank", settings)
        assert p.name == "omlx"

    def test_build_generate_openai_requires_api_key(self):
        """openai 后端无环境变量 → fail fast。"""
        settings = load_settings()
        # 模拟有 openai provider 配置但未设环境变量
        settings.llm.providers = {"openai": MagicMock(endpoint="https://api.openai.com/v1", api_key_env="FAKE_KEY_FOR_TEST")}
        settings.llm.generate = MagicMock(
            backend="openai", endpoint="https://api.openai.com/v1",
            api_key_env="FAKE_KEY_FOR_TEST", model="gpt-4o-mini",
            max_concurrency=1, max_timeout_retries=3, models={},
        )
        with pytest.raises(RuntimeError, match="FAKE_KEY_FOR_TEST.*未设置"):
            build_provider("generate", settings)

    def test_build_embed_rejects_non_omlx(self):
        """embed 后端不是 omlx → ValueError。"""
        settings = load_settings()
        settings.llm.embed = MagicMock(backend="openai")
        with pytest.raises(ValueError, match="embed.*强制本地"):
            build_provider("embed", settings)

    def test_build_rerank_rejects_non_omlx(self):
        """rerank 后端不是 omlx → ValueError（§4.8 外部暂不支持，早失败提示回退 omlx）。"""
        settings = load_settings()
        settings.llm.rerank = MagicMock(backend="openai")
        with pytest.raises(ValueError, match="rerank"):
            build_provider("rerank", settings)

    def test_build_unknown_capability(self):
        settings = load_settings()
        with pytest.raises(ValueError, match="不支持的 capability"):
            build_provider("translate", settings)  # type: ignore[arg-type]


# ── _resolve_api_key ───────────────────────────────────────────────

class TestResolveApiKey:
    def test_omlx_returns_none(self):
        assert _resolve_api_key(None, "omlx") is None
        assert _resolve_api_key("ANY_KEY", "omlx") is None  # omlx 忽略 env_name

    def test_openai_no_env_name_raises(self):
        with pytest.raises(RuntimeError, match="需要 api_key_env"):
            _resolve_api_key(None, "openai")

    def test_openai_missing_env_var_raises(self):
        # 确保这个 env var 不存在
        os.environ.pop("TEST_OPENAI_KEY_MISSING_12345", None)
        with pytest.raises(RuntimeError, match="TEST_OPENAI_KEY_MISSING_12345.*未设置"):
            _resolve_api_key("TEST_OPENAI_KEY_MISSING_12345", "openai")

    def test_openai_empty_env_var_raises(self):
        os.environ["TEST_OPENAI_KEY_EMPTY_12345"] = ""
        try:
            with pytest.raises(RuntimeError, match="未设置或为空"):
                _resolve_api_key("TEST_OPENAI_KEY_EMPTY_12345", "openai")
        finally:
            os.environ.pop("TEST_OPENAI_KEY_EMPTY_12345", None)

    def test_openai_valid_env_var_returns_key(self):
        os.environ["TEST_OPENAI_KEY_OK_12345"] = "sk-test-12345"
        try:
            result = _resolve_api_key("TEST_OPENAI_KEY_OK_12345", "openai")
            assert result == "sk-test-12345"
        finally:
            os.environ.pop("TEST_OPENAI_KEY_OK_12345", None)
