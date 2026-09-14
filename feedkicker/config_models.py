"""配置数据模型：路径常量与全部 dataclass（config.py 的 facade 数据面，#171）。"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_KNOWN_TOP_KEYS = frozenset([
    "feishu_webhook", "feishu_secret", "bootstrap_days", "http", "feeds",
    "site", "bitable", "salon", "minimax", "wiki", "extract",
])

_KNOWN_SECTION_KEYS = {
    "http": ("timeout_seconds", "user_agent"),
    "site": ("top_n",),
    "bitable": ("enabled", "app_token", "table_id", "url", "retention_days"),
    "salon": (
        "enabled", "app_token", "table_id", "wiki_space_id", "wiki_parent_token",
        "trigger_weekday", "trigger_hour", "trigger_minute",
    ),
    "minimax": ("api_key", "model", "base_url"),
    "wiki": ("space_id", "parent_token", "app_token"),
    "extract": (
        "enabled", "since_days", "batch_size", "provider", "prompt_file", "max_calls", "providers",
    ),
}


def warn_unknown_keys(raw: dict[str, Any]) -> None:
    """未知配置键与显式 enabled:false 仅 WARNING，不硬失败（本地残留键不得弄挂 prod 启动，#291）。"""
    for key in raw:
        if key not in _KNOWN_TOP_KEYS:
            log.warning("配置未知键：%s（已忽略）", key)
    for section, allowed in _KNOWN_SECTION_KEYS.items():
        spec = raw.get(section)
        if not isinstance(spec, dict):
            continue
        for key in spec:
            if key not in allowed:
                log.warning("配置未知键：%s.%s（已忽略）", section, key)
        if spec.get("enabled") is False:
            log.warning("配置段 %s.enabled=false，该功能已关闭", section)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "tc-prod.sqlite3"
VALID_ENVS = ("dev", "test", "prod")

PROVIDER_KEY_ENVS: dict[str, tuple[str, ...]] = {
    "minimax": ("MiniMax_Key", "MINIMAX_API_KEY"),
    "deepseek": ("DEEPSEEK_API_KEY",),
}


def env_key_for(provider: str) -> str:
    """provider key 环境变量查找（MiniMax_Key 优先 MINIMAX_API_KEY，按注册表顺序）。"""
    for name in PROVIDER_KEY_ENVS.get(provider, ()):
        value = os.environ.get(name)
        if value:
            return value
    return ""


@dataclass
class HttpConf:
    timeout_seconds: float = 20.0
    user_agent: str = "rss2feishu/0.1 (+local cron; private)"


@dataclass
class Feed:
    name: str
    url: str


@dataclass
class SiteConf:
    """GitHub Pages 链路已移除（site.enabled 全环境 false）；仅保留摘要卡每源条数。"""

    top_n: int = 5


@dataclass
class BitableConf:
    enabled: bool = False
    app_token: str = ""
    table_id: str = ""
    url: str = ""
    retention_days: int = 365


@dataclass
class SalonConf:
    enabled: bool = False
    app_token: str = ""
    table_id: str = ""
    wiki_space_id: str = ""
    wiki_parent_token: str = ""
    trigger_weekday: int = 4
    trigger_hour: int = 10
    trigger_minute: int = 0


@dataclass
class MinimaxConf:
    api_key: str = ""
    model: str = "MiniMax-M3"
    base_url: str = "https://api.minimaxi.com"


@dataclass
class WikiConf:
    space_id: str = ""
    parent_token: str = ""
    app_token: str = ""


@dataclass
class ProviderConf:
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    tool_label: str = ""


@dataclass
class ExtractConf:
    enabled: bool = False
    since_days: int = 7
    batch_size: int = 30
    provider: str = "minimax"
    prompt_file: str = "prompts/extract.md"
    max_calls: int = 0
    providers: dict[str, ProviderConf] = field(default_factory=dict)


@dataclass
class Config:
    app_env: str = "prod"
    feishu_webhook: str = ""
    feishu_secret: str = ""
    bootstrap_days: int = 3
    http: HttpConf = field(default_factory=HttpConf)
    feeds: list[Feed] = field(default_factory=list)
    db_path: Path = DEFAULT_DB_PATH
    site: SiteConf = field(default_factory=SiteConf)
    bitable: BitableConf = field(default_factory=BitableConf)
    salon: SalonConf = field(default_factory=SalonConf)
    minimax: MinimaxConf = field(default_factory=MinimaxConf)
    wiki: WikiConf = field(default_factory=WikiConf)
    extract: ExtractConf = field(default_factory=ExtractConf)
