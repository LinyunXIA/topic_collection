from __future__ import annotations

import os
from pathlib import Path

import yaml

from feedkicker.config_models import (
    DEFAULT_DB_PATH as DEFAULT_DB_PATH,
    PROJECT_ROOT as PROJECT_ROOT,
    VALID_ENVS as VALID_ENVS,
    BitableConf as BitableConf,
    Config as Config,
    Feed as Feed,
    HttpConf as HttpConf,
    MinimaxConf as MinimaxConf,
    SalonConf as SalonConf,
    SiteConf as SiteConf,
    WikiConf as WikiConf,
)


def db_path_for(app_env: str) -> Path:
    return PROJECT_ROOT / "data" / f"tc-{app_env}.sqlite3"


def config_path_for(app_env: str) -> Path:
    """默认配置锚定仓库根，不随调用方 cwd 漂移（#163）。"""
    return PROJECT_ROOT / f"config-{app_env}.yaml"


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
    seen_names: dict[str, str] = {}
    for i, item in enumerate(raw.get("feeds") or []):
        item = item or {}
        url = str(item.get("url") or "").strip()
        name = str(item.get("name") or "").strip()
        if not url:
            raise ValueError(f"feeds[{i}] 缺少 url")
        name = name or url
        if name in seen_names:
            raise ValueError(f"feeds 名称重复: {name}（{seen_names[name]} 与 {url}），name 必须唯一")
        seen_names[name] = url
        feeds.append(Feed(name=name, url=url))
    cfg.feeds = feeds

    site_raw = raw.get("site") or {}
    cfg.site = SiteConf(top_n=max(1, int(site_raw.get("top_n", cfg.site.top_n))))

    bt_raw = raw.get("bitable") or {}
    cfg.bitable = BitableConf(
        enabled=bool(bt_raw.get("enabled", cfg.bitable.enabled)),
        app_token=str(bt_raw.get("app_token") or ""),
        table_id=str(bt_raw.get("table_id") or ""),
        url=str(bt_raw.get("url") or ""),
        retention_days=max(1, int(bt_raw.get("retention_days", cfg.bitable.retention_days))),
    )

    salon_raw = raw.get("salon") or {}
    cfg.salon = SalonConf(
        enabled=bool(salon_raw.get("enabled", cfg.salon.enabled)),
        app_token=str(salon_raw.get("app_token") or ""),
        table_id=str(salon_raw.get("table_id") or ""),
        wiki_space_id=str(salon_raw.get("wiki_space_id") or ""),
        wiki_parent_token=str(salon_raw.get("wiki_parent_token") or ""),
        trigger_weekday=int(salon_raw.get("trigger_weekday", cfg.salon.trigger_weekday)),
        trigger_hour=int(salon_raw.get("trigger_hour", cfg.salon.trigger_hour)),
        trigger_minute=int(salon_raw.get("trigger_minute", cfg.salon.trigger_minute)),
    )

    minimax_raw = raw.get("minimax") or {}
    cfg.minimax = MinimaxConf(
        api_key=str(minimax_raw.get("api_key") or ""),
        model=str(minimax_raw.get("model") or cfg.minimax.model),
        base_url=str(minimax_raw.get("base_url") or cfg.minimax.base_url),
    )

    wiki_raw = raw.get("wiki") or {}
    cfg.wiki = WikiConf(
        space_id=str(wiki_raw.get("space_id") or salon_raw.get("wiki_space_id") or ""),
        parent_token=str(wiki_raw.get("parent_token") or salon_raw.get("wiki_parent_token") or ""),
        app_token=str(wiki_raw.get("app_token") or ""),
    )

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
