"""配置数据模型：路径常量与全部 dataclass（config.py 的 facade 数据面，#171）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "tc-prod.sqlite3"
VALID_ENVS = ("dev", "test", "prod")


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
