from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path("config-prod.yaml")
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
    enabled: bool = True
    base_url: str = "https://linyunxia.github.io/topic_collection"
    repo: str = "LinyunXIA/topic_collection"
    branch: str = "gh-pages"
    top_n: int = 5


@dataclass
class BitableConf:
    enabled: bool = False
    app_token: str = ""
    table_id: str = ""
    url: str = ""


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
    cfg.site = SiteConf(
        enabled=bool(site_raw.get("enabled", cfg.site.enabled)),
        base_url=str(site_raw.get("base_url") or cfg.site.base_url).rstrip("/"),
        repo=str(site_raw.get("repo") or cfg.site.repo),
        branch=str(site_raw.get("branch") or cfg.site.branch),
        top_n=max(1, int(site_raw.get("top_n", cfg.site.top_n))),
    )

    bt_raw = raw.get("bitable") or {}
    cfg.bitable = BitableConf(
        enabled=bool(bt_raw.get("enabled", cfg.bitable.enabled)),
        app_token=str(bt_raw.get("app_token") or ""),
        table_id=str(bt_raw.get("table_id") or ""),
        url=str(bt_raw.get("url") or ""),
    )

    env_webhook = os.environ.get("FEISHU_WEBHOOK")
    if env_webhook:
        cfg.feishu_webhook = env_webhook

    env_secret = os.environ.get("FEISHU_SECRET")
    if env_secret:
        cfg.feishu_secret = env_secret

    env_site_enabled = os.environ.get("TC_SITE_ENABLED")
    if env_site_enabled is not None and env_site_enabled != "":
        cfg.site.enabled = env_site_enabled.strip().lower() not in ("0", "false", "no", "off")

    env_db = os.environ.get("TC_DB")
    if env_db:
        cfg.db_path = Path(env_db)
    if db_path:
        cfg.db_path = Path(db_path)
    if not env_db and not db_path:
        cfg.db_path = db_path_for(env)

    return cfg
