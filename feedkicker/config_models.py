"""配置数据模型：路径常量与全部 dataclass（config.py 的 facade 数据面，#171）。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

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
