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
    test_dsn: str = Field(
        default="postgresql+asyncpg://tc:tc@localhost:5434/topic_collection_test",
        description="测试 DSN，TC_APP_ENV=test 时生效（docker postgres-test 5434）",
    )
    prod_dsn: str | None = Field(
        default=None,
        description="生产 DSN，可含 ${POSTGRES_PASSKEY} 占位，需 TC_APP_ENV=prod 时生效",
    )
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
    """生成能力配置（per-capability，DESIGN §4）。

    per-task 模型覆盖统一走顶层 LLMSettings.models（fix #9.3）——本类不再
    单独声明 models 字段以避免双重真源；services 只读 settings.llm.models.get(task)。
    """

    backend: str = "omlx"
    endpoint: str = "http://localhost:8000"
    api_key_env: str | None = None
    model: str = "Qwen3.8-27B-MLX-4bit"
    max_concurrency: int = 1
    max_timeout_retries: int = 3


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
    api_key_env: str | None = None  # 统一命名：嵌套 GenerateSettings 等都叫 api_key_env
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


class FeishuSettings(BaseSettings):
    """飞书 Webhook 推送配置（DESIGN §10.4 / PRD §15 #8）

    webhook 本体含 token，永不写 yaml/DB，仅通过环境变量读取，
    webhook_env 为环境变量名（默认 FEISHU_WEBHOOK）。
    """

    enabled: bool = False
    webhook_env: str = "FEISHU_WEBHOOK"
    events: list[str] = Field(default_factory=lambda: ["daily", "weekly"])


class ScheduleSettings(BaseSettings):
    daily_report: str = "08:00"
    weekly_report: str = "Mon 08:00"
    feishu: FeishuSettings = Field(default_factory=FeishuSettings)


class Settings(BaseSettings):
    """顶层配置，从 config.yaml 加载，环境变量可覆盖。"""

    model_config = SettingsConfigDict(
        env_prefix="TC_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    app_env: str = Field(default="dev", description="运行环境 dev|test|prod，TC_APP_ENV 覆盖")
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


def _resolve_env_dsn(settings: Settings) -> None:
    """解析环境 DSN：test/prod 覆盖 dsn（DESIGN §5.4.1）。

    - test: TC_APP_ENV=test 时 dsn = test_dsn（默认 5434/topic_collection_test）
    - prod: TC_APP_ENV=prod 时处理 prod_dsn 占位与 POSTGRES_PASSKEY 注入
    - dev: 保持默认 dsn（5433/topic_collection）
    """
    import os

    if settings.app_env == "test":
        # test_dsn 可含 ${POSTGRES_PASSKEY} 占位（与 prod 同逻辑）
        test_dsn = settings.db.test_dsn
        if test_dsn and ("${POSTGRES_PASSKEY}" in test_dsn or "$POSTGRES_PASSKEY" in test_dsn):
            pwd = os.environ.get("POSTGRES_PASSKEY")
            if pwd:
                test_dsn = test_dsn.replace("${POSTGRES_PASSKEY}", pwd).replace("$POSTGRES_PASSKEY", pwd)
            else:
                test_dsn = test_dsn.replace(":${POSTGRES_PASSKEY}", "").replace("${POSTGRES_PASSKEY}", "").replace(":$POSTGRES_PASSKEY", "").replace("$POSTGRES_PASSKEY", "")
            settings.db.test_dsn = test_dsn
        if test_dsn:
            settings.db.dsn = test_dsn
        return

    if settings.app_env != "prod":
        return
    # 已有 prod_dsn（可能来自 TC_DB__PROD_DSN 或 config.yaml db.prod_dsn）
    prod_dsn = settings.db.prod_dsn
    if prod_dsn:
        # 替换占位；无密码则信任连接（去掉 :${POSTGRES_PASSKEY}）
        if "${POSTGRES_PASSKEY}" in prod_dsn or "$POSTGRES_PASSKEY" in prod_dsn:
            pwd = os.environ.get("POSTGRES_PASSKEY")
            if pwd:
                prod_dsn = prod_dsn.replace("${POSTGRES_PASSKEY}", pwd).replace("$POSTGRES_PASSKEY", pwd)
            else:
                # 去掉占位及前面的冒号，避免 postgres:@localhost
                prod_dsn = prod_dsn.replace(":${POSTGRES_PASSKEY}", "").replace("${POSTGRES_PASSKEY}", "").replace(":$POSTGRES_PASSKEY", "").replace("$POSTGRES_PASSKEY", "")
            settings.db.prod_dsn = prod_dsn
        # 生效：覆盖 dsn
        settings.db.dsn = prod_dsn
        return
    # 未配 prod_dsn，尝试用 POSTGRES_PASSKEY 构造默认；无密码则信任连接
    pwd = os.environ.get("POSTGRES_PASSKEY")
    if pwd:
        settings.db.dsn = f"postgresql+asyncpg://postgres:{pwd}@localhost:5432/topic_collection"
    else:
        settings.db.dsn = "postgresql+asyncpg://postgres@localhost:5432/topic_collection"
    settings.db.prod_dsn = settings.db.dsn
    return


def _resolve_prod_dsn(settings: Settings) -> None:
    """兼容旧名：转发至 _resolve_env_dsn。"""
    return _resolve_env_dsn(settings)


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
    settings = Settings.model_validate(file_data)
    # TC_APP_ENV 需显式覆盖 file_data（model_validate 的 file_data 优先于 env，需手动处理）
    if "TC_APP_ENV" in os.environ:
        settings.app_env = os.environ["TC_APP_ENV"]
    # TC_DB__TEST_DSN / TC_DB__PROD_DSN 同理（嵌套 env，需手动覆盖 file_data 的占位）
    if "TC_DB__TEST_DSN" in os.environ:
        settings.db.test_dsn = os.environ["TC_DB__TEST_DSN"]
    elif "TC_DB_TEST_DSN" in os.environ:
        settings.db.test_dsn = os.environ["TC_DB_TEST_DSN"]
    if "TC_DB__PROD_DSN" in os.environ:
        settings.db.prod_dsn = os.environ["TC_DB__PROD_DSN"]
    elif "TC_DB_PROD_DSN" in os.environ:
        settings.db.prod_dsn = os.environ["TC_DB_PROD_DSN"]
    _resolve_env_dsn(settings)
    return settings
