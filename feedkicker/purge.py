"""tc-purge：365 天滚动保留 CLI（sqlite + 多维表格双清）。

安全模型：
- 默认 dry-run 只打印 PurgeStats，真删需显式 --apply；月度 launchd 只调度 dry-run；
- sqlite 仅删已归档到 bitable 的行（bitable_synced_at IS NOT NULL），
  超期未归档只计数 WARNING（salon 占位行 pushed_at 为 NULL 天然不匹配）；
- bitable 侧按「推送时间」早于 cutoff 删记录（bitable_purge），
  仅操作 cfg.bitable 资讯归档 Base，绝不调 ensure_initialized（防误建 Base）。
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from feedkicker import bitable_purge, store
from feedkicker.config import Config, load_config

log = logging.getLogger(__name__)

PURGE_LAST_RUN_KEY = "purge_last_run_at"


@dataclass
class PurgeStats:
    dry_run: bool
    retention_days: int
    cutoff_iso: str
    cutoff_date_shanghai: str
    sqlite_deleted: int = 0
    sqlite_expired_unarchived: int = 0
    bitable_scanned: int = 0
    bitable_expired: int = 0
    bitable_deleted: int = 0
    bitable_skipped_reason: str = ""


def cutoff_iso(days: int, now: datetime | None = None) -> str:
    """UTC %Y-%m-%dT%H:%M:%SZ 截止串（字典序可比，先例 promise_skip_old）。"""
    ref = now if now is not None else datetime.now(UTC)
    return (ref - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def purge_sqlite(
    conn: sqlite3.Connection, cutoff: str, dry_run: bool
) -> tuple[int, int]:
    """删 pushed_at < cutoff 且 bitable_synced_at 非空的行，返回 (deleted, unarchived)。"""
    rows = conn.execute(
        "SELECT feed_id, entry_key, bitable_synced_at FROM articles"
        " WHERE pushed_at IS NOT NULL AND pushed_at < ?",
        (cutoff,),
    ).fetchall()
    archivable = [(r[0], r[1]) for r in rows if r[2]]
    unarchived = len(rows) - len(archivable)
    if unarchived:
        log.warning("purge：%d 条超期但未归档 bitable，保留不删", unarchived)
    if dry_run or not archivable:
        return 0, unarchived
    conn.executemany("DELETE FROM articles WHERE feed_id = ? AND entry_key = ?", archivable)
    conn.commit()
    return len(archivable), unarchived


def _bitable_guard(cfg: Config) -> str:
    """返回跳过原因；空串表示可以执行 bitable 清理。"""
    bt = cfg.bitable
    if not bt.enabled:
        return "bitable未启用"
    if not bt.app_token or not bt.table_id:
        return "app_token/table_id 缺失"
    if "<" in bt.app_token or "<" in bt.table_id:
        return "占位 token 未替换"
    return ""


def run(
    cfg: Config,
    conn: sqlite3.Connection,
    dry_run: bool,
    retention_days: int | None = None,
    now: datetime | None = None,
) -> PurgeStats:
    days = retention_days if retention_days is not None else cfg.bitable.retention_days
    cutoff = cutoff_iso(days, now=now)
    cutoff_date = bitable_purge.cutoff_date_shanghai(days, now=now)
    stats = PurgeStats(
        dry_run=dry_run,
        retention_days=days,
        cutoff_iso=cutoff,
        cutoff_date_shanghai=cutoff_date,
    )

    stats.sqlite_deleted, stats.sqlite_expired_unarchived = purge_sqlite(conn, cutoff, dry_run)

    reason = _bitable_guard(cfg)
    if reason:
        stats.bitable_skipped_reason = reason
        log.info("purge：跳过 bitable 清理（%s）", reason)
    else:
        deleted, expired, scanned = bitable_purge.purge_expired_records(
            cfg.bitable.app_token, cfg.bitable.table_id, cutoff_date, dry_run=dry_run
        )
        stats.bitable_deleted = deleted
        stats.bitable_expired = expired
        stats.bitable_scanned = scanned

    if not dry_run:
        store.set_meta(
            conn,
            PURGE_LAST_RUN_KEY,
            (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    print(json.dumps(asdict(stats), ensure_ascii=False, indent=2))
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tc-purge",
        description="365 天滚动保留：清理超期 sqlite/bitable 记录（默认 dry-run，--apply 才真删）",
    )
    parser.add_argument("--apply", action="store_true", help="真删；缺省仅 dry-run 巡检")
    parser.add_argument(
        "--retention-days",
        type=int,
        default=None,
        help="保留天数（默认取 config.bitable.retention_days=365）",
    )
    parser.add_argument("--config", default=None, help="指定 config-{env}.yaml 路径")
    parser.add_argument("--db", default=None, help="sqlite 路径（覆盖 TC_DB 与 --env 推导）")
    parser.add_argument(
        "--env",
        default=None,
        choices=["dev", "test", "prod"],
        help="运行环境，决定默认配置文件与 db 路径（覆盖 TC_APP_ENV）",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        cfg = load_config(args.config, args.db, app_env=args.env)
    except Exception as e:  # noqa: BLE001
        log.error("%s", e)
        return 2

    conn = store.connect(cfg.db_path)
    log.info(
        "purge 运行开始：环境=%s，db=%s，dry_run=%s",
        cfg.app_env,
        cfg.db_path,
        not args.apply,
    )
    try:
        run(cfg, conn, dry_run=not args.apply, retention_days=args.retention_days)
    except Exception:  # noqa: BLE001
        log.exception("未捕获异常")
        return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
