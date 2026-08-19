"""应用配置 — pydantic-settings + YAML，环境变量覆盖（DESIGN §9）"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DBSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TC_DB_")

    dsn: str = "postgresql+asyncpg://tc:tc@localhost:5433/topic_collection"
    pool_size: int = 5
    vector_dim: int = 1536


class WebSettings(BaseSettings):
    host: str = "127.0.0.1"
    port: int = 7111


class ProviderConfig(BaseSettings):
    """单个外部 LLM provider 的配置（endpoint + api_key + patch）。"""

    endpoint: str
    api_key_env: str | None = None
    patch: dict = Field(default_factory=dict)  # ProviderPatch 字段，如 {strip_think_tags: true}


class GenerateSettings(BaseSettings):
    """生成能力配置（per-capability，DESIGN §4）。"""

    backend: str = "omlx"
    endpoint: str = "http://localhost:8000"
    api_key_env: str | None = None
    model: str = "Qwen3.8-27B-MLX-4bit"
    max_concurrency: int = 1
    max_timeout_retries: int = 3
    models: dict[str, str] = Field(default_factory=dict)


class EmbedSettings(BaseSettings):
    backend: str = "omlx"
    endpoint: str = "http://localhost:8000"
    api_key_env: str | None = None
    model: str = "Qwen3-Embedding-8B-4bit-DWQ"
    max_tokens: int = 8192


class RerankSettings(BaseSettings):
    backend: str = "omlx"
    endpoint: str = "http://localhost:8000"
    api_key_env: str | None = None
    model: str = "Qwen3-Reranker-4B-mxfp8"


class LLMSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TC_LLM_")

    backend: str = "omlx"
    endpoint: str = "http://localhost:8000"
    api_key: str | None = Field(default=None, alias="api_key_env")
    model: str = "Qwen3.8-27B-MLX-4bit"
    max_concurrency: int = 1
    max_timeout_retries: int = 3
    models: dict[str, str] = Field(default_factory=dict)
    generate: GenerateSettings | None = None  # None = 用旧顶层字段（向后兼容）
    embed: EmbedSettings = Field(default_factory=EmbedSettings)
    rerank: RerankSettings = Field(default_factory=RerankSettings)
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)


class DedupSettings(BaseSettings):
    threshold: float = 0.95
    window_days: int = 30
    k: int = 10
    same_lang_only: bool = True


class IngestionSettings(BaseSettings):
    fetch_interval_hours: int = 6
    user_agent: str = "TopicCollection/0.1 (+local personal KB)"
    max_scrape_bytes: int = 5242880
    feed_disable_after: int = 5
    max_items_per_fetch: int = 50
    global_concurrency: int = 8
    per_host_interval_ms: int = 500
    fetch_events_retention_days: int = 90
    dedup: DedupSettings = Field(default_factory=DedupSettings)


class TopicsSettings(BaseSettings):
    llm_threshold: float = 0.6
    reclassify_recent_days: int = 30


class ScheduleSettings(BaseSettings):
    daily_report: str = "08:00"
    weekly_report: str = "Mon 08:00"


class Settings(BaseSettings):
    """顶层配置，从 config.yaml 加载，环境变量可覆盖。"""

    model_config = SettingsConfigDict(
        env_prefix="TC_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    data_dir: str = "./data"
    db: DBSettings = Field(default_factory=DBSettings)
    web: WebSettings = Field(default_factory=WebSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    ingestion: IngestionSettings = Field(default_factory=IngestionSettings)
    topics: TopicsSettings = Field(default_factory=TopicsSettings)
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并 override 到 base（override 优先）。"""
    merged = base.copy()
    for k, v in override.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k] = _deep_merge(merged[k], v)
        else:
            merged[k] = v
    return merged


def load_settings(config_path: str | Path | None = None) -> Settings:
    """加载配置：config.yaml → 环境变量覆盖。"""
    import os

    if config_path is None:
        config_path = os.environ.get("TC_CONFIG", "./config/config.yaml")
    p = Path(config_path)
    file_data: dict[str, Any] = {}
    if p.exists():
        with open(p) as f:
            file_data = yaml.safe_load(f) or {}
    return Settings.model_validate(file_data)
