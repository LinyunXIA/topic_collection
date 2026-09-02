from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "tc-prod.sqlite3"
VALID_ENVS = ("dev", "test", "prod")


def db_path_for(app_env: str) -> Path:
    return PROJECT_ROOT / "data" / f"tc-{app_env}.sqlite3"


def config_path_for(app_env: str) -> Path:
    return Path(f"config-{app_env}.yaml")


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
    # GitHub Pages 链路已移除（site.enabled 全环境 false）；仅保留摘要卡每源条数
    top_n: int = 5


@dataclass
class BitableConf:
    enabled: bool = False
    app_token: str = ""
    table_id: str = ""
    url: str = ""


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


def load_config(
    config_path: str | Path | None = None,
    db_path: str | Path | None = None,
    app_env: str | None = None,
) -> Config:
    env = app_env or os.environ.get("TC_APP_ENV") or "prod"
    if env not in VALID_ENVS:
        raise ValueError(f"未知环境: {env}（可选 {VALID_ENVS}）")

    path = Path(config_path) if config_path else config_path_for(env)
    if not path.exists():
        raise FileNotFoundError(
            f"配置文件不存在: {path}（未指定 --config 时默认查找 config-{env}.yaml）"
        )

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = Config(app_env=env)
    cfg.feishu_webhook = str(raw.get("feishu_webhook") or "")
    cfg.feishu_secret = str(raw.get("feishu_secret") or "")
    cfg.bootstrap_days = int(raw.get("bootstrap_days", cfg.bootstrap_days))

    http_raw = raw.get("http") or {}
    cfg.http = HttpConf(
        timeout_seconds=float(http_raw.get("timeout_seconds", cfg.http.timeout_seconds)),
        user_agent=str(http_raw.get("user_agent") or cfg.http.user_agent),
    )

    feeds: list[Feed] = []
    for i, item in enumerate(raw.get("feeds") or []):
        item = item or {}
        url = str(item.get("url") or "").strip()
        name = str(item.get("name") or "").strip()
        if not url:
            raise ValueError(f"feeds[{i}] 缺少 url")
        feeds.append(Feed(name=name or url, url=url))
    cfg.feeds = feeds

    site_raw = raw.get("site") or {}
    cfg.site = SiteConf(top_n=max(1, int(site_raw.get("top_n", cfg.site.top_n))))

    bt_raw = raw.get("bitable") or {}
    cfg.bitable = BitableConf(
        enabled=bool(bt_raw.get("enabled", cfg.bitable.enabled)),
        app_token=str(bt_raw.get("app_token") or ""),
        table_id=str(bt_raw.get("table_id") or ""),
        url=str(bt_raw.get("url") or ""),
    )

    salon_raw = raw.get("salon") or {}
    cfg.salon = SalonConf(
        enabled=bool(salon_raw.get("enabled", cfg.salon.enabled)),
        app_token=str(salon_raw.get("app_token") or ""),
        table_id=str(salon_raw.get("table_id") or ""),
        wiki_space_id=str(salon_raw.get("wiki_space_id") or salon_raw.get("wiki_space") or ""),
        wiki_parent_token=str(salon_raw.get("wiki_parent_token") or ""),
        trigger_weekday=int(salon_raw.get("trigger_weekday", cfg.salon.trigger_weekday)),
        trigger_hour=int(salon_raw.get("trigger_hour", cfg.salon.trigger_hour)),
        trigger_minute=int(salon_raw.get("trigger_minute", cfg.salon.trigger_minute)),
    )

    minimax_raw = raw.get("minimax") or {}
    if not minimax_raw and salon_raw.get("minimax_api_key"):
        minimax_raw = {"api_key": salon_raw.get("minimax_api_key")}
    cfg.minimax = MinimaxConf(
        api_key=str(minimax_raw.get("api_key") or minimax_raw.get("minimax_api_key") or ""),
        model=str(minimax_raw.get("model") or cfg.minimax.model),
        base_url=str(minimax_raw.get("base_url") or cfg.minimax.base_url),
    )

    wiki_raw = raw.get("wiki") or {}
    cfg.wiki = WikiConf(
        space_id=str(wiki_raw.get("space_id") or wiki_raw.get("wiki_space_id") or salon_raw.get("wiki_space_id") or ""),
        parent_token=str(wiki_raw.get("parent_token") or wiki_raw.get("wiki_parent_token") or salon_raw.get("wiki_parent_token") or ""),
        app_token=str(wiki_raw.get("app_token") or ""),
    )
    if not cfg.wiki.space_id and cfg.salon.wiki_space_id:
        cfg.wiki.space_id = cfg.salon.wiki_space_id
    if not cfg.wiki.parent_token and cfg.salon.wiki_parent_token:
        cfg.wiki.parent_token = cfg.salon.wiki_parent_token

    env_minimax = os.environ.get("MiniMax_Key") or os.environ.get("MINIMAX_API_KEY")
    if env_minimax:
        cfg.minimax.api_key = env_minimax
    if cfg.minimax.api_key and cfg.minimax.api_key.strip().startswith("<"):
        cfg.minimax.api_key = ""

    env_salon_token = os.environ.get("TC_SALON_TOKEN")
    if env_salon_token:
        cfg.salon.app_token = env_salon_token

    env_webhook = os.environ.get("FEISHU_WEBHOOK")
    if env_webhook:
        cfg.feishu_webhook = env_webhook

    env_secret = os.environ.get("FEISHU_SECRET")
    if env_secret:
        cfg.feishu_secret = env_secret

    env_db = os.environ.get("TC_DB")
    if env_db:
        cfg.db_path = Path(env_db)
    if db_path:
        cfg.db_path = Path(db_path)
    if not env_db and not db_path:
        cfg.db_path = db_path_for(env)

    return cfg
