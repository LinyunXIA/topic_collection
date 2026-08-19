"""LLM Provider Factory — 按能力构建 provider

per-capability backend 选择：generate / embed / rerank 各自独立，
embed/rerank 强制本地 omlx（隐私，DESIGN §4.3/§12）。

用法：
    provider = build_provider("generate", settings)
    llm_client = LLMClient(provider, max_concurrency=1)
"""

from __future__ import annotations

import logging
import os
from typing import Literal

from app.config import LLMSettings, Settings
from app.llm.base import LLMProvider
from app.llm.patches import ProviderPatch

logger = logging.getLogger(__name__)


def _resolve_api_key(env_name: str | None, backend: str) -> str | None:
    """从环境变量读取 API key。

    - omlx 后端：env_name 可为 None，返回 None（oMLX 不鉴权）
    - openai 后端：env_name 必须设置且非空，否则 fail fast
    """
    if backend == "omlx":
        return None  # oMLX 不鉴权，跳过 resolve
    if not env_name:
        raise RuntimeError(
            f"后端 '{backend}' 需要 api_key_env 配置项"
        )
    key = os.environ.get(env_name)
    if not key:
        raise RuntimeError(
            f"环境变量 {env_name} 未设置或为空；"
            f"请先 export {env_name}=sk-... 后再启动"
        )
    return key


def _dict_to_patch(patch_dict: dict | None) -> ProviderPatch | None:
    """将 config dict 转为 ProviderPatch dataclass。None 或空 dict → None。"""
    if not patch_dict:
        return None
    # 过滤掉 ProviderPatch 不认识的 key（容错）
    valid_keys = {f.name for f in ProviderPatch.__dataclass_fields__.values()}
    filtered = {k: v for k, v in patch_dict.items() if k in valid_keys}
    return ProviderPatch(**filtered) if filtered else None


def _build_omlx(
    endpoint: str,
    api_key_env: str | None,
    generation_model: str,
    embedding_model: str,
    rerank_model: str | None,
    patch: ProviderPatch | None = None,
) -> LLMProvider:
    """构造 oMLX provider（本机不鉴权，api_key_env 不用 resolve）。"""
    from app.llm.omlx import OMLXProvider

    return OMLXProvider(
        base_url=endpoint,
        api_key=None,  # oMLX 不鉴权，api_key_env 字段保留但不 resolve
        generation_model=generation_model,
        embedding_model=embedding_model,
        rerank_model=rerank_model,
        patch=patch,
    )


def _build_openai(
    endpoint: str,
    api_key_env: str | None,
    generation_model: str,
    embedding_model: str,
    patch: ProviderPatch | None = None,
) -> LLMProvider:
    """构造 OpenAI 兼容 provider（外部 API 必鉴权，fail fast）。"""
    from app.llm.openai import OpenAIProvider

    api_key = _resolve_api_key(api_key_env, "openai")
    return OpenAIProvider(
        base_url=endpoint,
        api_key=api_key,
        generation_model=generation_model,
        embedding_model=embedding_model,
        rerank_model=None,  # OpenAI 不支持 rerank
        patch=patch,
    )


def build_provider(
    capability: Literal["generate", "embed", "rerank"],
    settings: Settings | LLMSettings,
) -> LLMProvider:
    """根据能力类型和配置构建对应的 LLMProvider。

    - generate：可选 omlx | openai（从 settings.generate 或顶层字段读取）
    - embed：强制 omlx（本地，隐私）
    - rerank：强制 omlx（本地，隐私）

    Raises:
        ValueError: unknown backend
        RuntimeError: API key 缺失（外部 provider，fail fast）
    """
    llm: LLMSettings = settings.llm if isinstance(settings, Settings) else settings

    if capability == "generate":
        # 优先读 settings.generate 嵌套；None 则用旧顶层字段（向后兼容）
        gen = llm.generate
        if gen is not None:
            backend = gen.backend
            endpoint = gen.endpoint
            api_key_env = gen.api_key_env
            model = gen.model
            models = gen.models
            max_concurrency = gen.max_concurrency
            max_timeout_retries = gen.max_timeout_retries
        else:
            backend = llm.backend
            endpoint = llm.endpoint
            api_key_env = llm.api_key
            model = llm.model
            models = llm.models
            max_concurrency = llm.max_concurrency
            max_timeout_retries = llm.max_timeout_retries

        if backend == "omlx":
            return _build_omlx(
                endpoint=endpoint,
                api_key_env=api_key_env,
                generation_model=model,
                embedding_model=llm.embed.model,
                rerank_model=llm.rerank.model,
            )
        elif backend in llm.providers:
            # 任何在 providers 注册表里的后端都走 OpenAI 兼容协议
            provider_cfg = llm.providers[backend]
            return _build_openai(
                endpoint=provider_cfg.endpoint,
                api_key_env=provider_cfg.api_key_env,
                generation_model=model,
                embedding_model=llm.embed.model,
                patch=_dict_to_patch(provider_cfg.patch),
            )
        else:
            raise ValueError(
                f"generate 能力不支持 backend '{backend}'；"
                f"可用：omlx（本地）或 providers 注册表中的 key（{list(llm.providers.keys())}）"
            )

    elif capability == "embed":
        # 强制本地
        cfg = llm.embed
        if cfg.backend != "omlx":
            raise ValueError(
                f"embed 能力强制本地 omlx，不支持 '{cfg.backend}'；"
                f"请勿修改 embed.backend 配置"
            )
        return _build_omlx(
            endpoint=cfg.endpoint,
            api_key_env=cfg.api_key_env,
            generation_model="",  # embed 不用 generation_model
            embedding_model=cfg.model,
            rerank_model=None,
        )

    elif capability == "rerank":
        # 强制本地
        cfg = llm.rerank
        if cfg.backend != "omlx":
            raise ValueError(
                f"rerank 能力强制本地 omlx，不支持 '{cfg.backend}'；"
                f"请勿修改 rerank.backend 配置"
            )
        return _build_omlx(
            endpoint=cfg.endpoint,
            api_key_env=cfg.api_key_env,
            generation_model="",  # rerank 不用 generation_model
            embedding_model="",   # rerank 不用 embedding_model
            rerank_model=cfg.model,
        )

    else:
        raise ValueError(f"不支持的 capability: '{capability}'（仅 generate | embed | rerank）")
