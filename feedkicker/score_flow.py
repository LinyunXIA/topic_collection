"""F38 tc-score CLI 骨架：读表 → 目标列校验 → 组批 → dry-run 计划（DESIGN §26.10）。"""

from __future__ import annotations

import argparse
import logging
import sys

from feedkicker import bitable_lark, score_source
from feedkicker.config import load_config
from feedkicker.config_models import Config
from feedkicker.log_setup import setup_logging

log = logging.getLogger(__name__)

PROVIDER_COLUMNS: dict[str, tuple[str, str]] = {
    "minimax": ("MMax打分", "MMax理由"),
    "deepseek": ("DS打分", "DS理由"),
}


def _token_missing(value: str) -> bool:
    return not value or "<" in value


def _non_negative_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"必须是非负整数: {value}") from None
    if n < 0:
        raise argparse.ArgumentTypeError(f"必须是非负整数: {value}")
    return n


def _field_names(app_token: str, table_id: str) -> set[str]:
    proc = bitable_lark._run(
        ["base", "+field-list", "--base-token", app_token, "--table-id", table_id], timeout=60
    )
    if not bitable_lark._ok(proc):
        raise RuntimeError("读取目标表字段列表失败，无法校验打分列")
    data = bitable_lark._data(proc)
    items = data.get("fields") or data.get("items") or []
    if not isinstance(items, list) or not all(isinstance(f, dict) for f in items):
        raise RuntimeError(f"field-list 响应 fields 非 list[dict]: {str(items)[:200]}")
    return {str(f.get("field_name") or f.get("name") or "") for f in items}


def ensure_columns(app_token: str, table_id: str, provider: str) -> int:
    """校验 provider 目标两列存在，缺任一 rc2 且不自动建列（DESIGN §26.6/§26.8）。"""
    score_col, reason_col = PROVIDER_COLUMNS[provider]
    names = _field_names(app_token, table_id)
    missing = [col for col in (score_col, reason_col) if col not in names]
    if missing:
        log.error("目标列缺失：%s（provider=%s），请先建列后重试；本次不写表", "、".join(missing), provider)
        return 2
    return 0


def run(
    cfg: Config,
    *,
    apply: bool,
    provider: str | None = None,
    limit: int = 0,
    max_calls: int = 0,
    force: bool = False,
) -> int:
    """读全表 → 校验目标列 → 组批 → dry-run 计划打印；rc 2 配置/参数非法、0 正常。

    本阶段（F38）不调 LLM、不写表：`--apply` 显式拒绝为 rc2（写入属 F41）；provider 目标
    列缺失亦 rc2、绝不自动建列。待打分/跳过统计留 F41 由 `plan_pending` 同源接口补齐。
    """
    if apply:
        log.error("写入尚未实现（F41）")
        return 2
    provider = (provider or cfg.score.provider or "minimax").strip()
    if provider not in PROVIDER_COLUMNS:
        log.error("未知 provider: %s（可用 %s）", provider, sorted(PROVIDER_COLUMNS))
        return 2
    if limit < 0 or max_calls < 0:
        log.error("参数非法：limit=%s max_calls=%s（须 ≥0）", limit, max_calls)
        return 2
    if not cfg.score.enabled:
        log.warning("score.enabled=false；显式 CLI 调用仍执行")
    if _token_missing(cfg.salon.app_token) or _token_missing(cfg.salon.table_id):
        log.error("salon app_token/table_id 缺失或为占位，无法读取/校验话题表")
        return 2
    app_token, table_id = cfg.salon.app_token, cfg.salon.table_id
    rows = score_source.read_rows(app_token, table_id, limit)
    if ensure_columns(app_token, table_id, provider) != 0:
        return 2
    batches = score_source.group_batches(rows, cfg.score.batch_size)
    pending, skipped = score_source.plan_pending(rows, provider)
    print(f"tc-score dry-run 计划：总行数={len(rows)} 批数={len(batches)} 待打分={len(pending)} 跳过={len(skipped)}")
    log.info(
        "tc-score 运行计划：环境=%s db=%s 目标表=%s provider=%s 总行数=%d 批数=%d 待打分=%d 跳过=%d max_calls=%s force=%s",
        cfg.app_env, cfg.db_path, table_id, provider,
        len(rows), len(batches), len(pending), len(skipped), max_calls, force,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tc-score",
        description="沙龙话题清单自动打分（默认 dry-run；本阶段仅打印计划）",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="写回打分/理由列（尚未实现，rc2）")
    mode.add_argument("--dry-run", action="store_true", help="仅打印计划（默认行为）")
    parser.add_argument(
        "--provider", choices=sorted(PROVIDER_COLUMNS), default=None,
        help="LLM provider（默认取 config score.provider，默认 minimax）",
    )
    parser.add_argument("--limit", type=_non_negative_int, default=0, help="最多处理行数（0=全部）")
    parser.add_argument("--max-calls", type=_non_negative_int, default=0, help="LLM 调用上限（0=不限）")
    parser.add_argument("--force", action="store_true", help="忽略既有分重算（F41）")
    parser.add_argument("--config", default=None, help="指定 config-{env}.yaml 路径")
    parser.add_argument("--db", default=None, help="sqlite 路径（覆盖 TC_DB 与 --env 推导）")
    parser.add_argument("--env", default=None, choices=["dev", "test", "prod"], help="运行环境，决定默认配置文件与 db 路径")
    args = parser.parse_args(argv)

    setup_logging(logging.INFO)
    try:
        cfg = load_config(args.config, args.db, app_env=args.env)
    except Exception as e:  # noqa: BLE001
        log.error("%s", e)
        return 2

    log.info("tc-score 运行开始：环境=%s，db=%s，mode=%s", cfg.app_env, cfg.db_path, "apply" if args.apply else "dry-run")
    try:
        return run(
            cfg,
            apply=args.apply,
            provider=args.provider,
            limit=args.limit,
            max_calls=args.max_calls,
            force=args.force,
        )
    except Exception:  # noqa: BLE001
        log.exception("未捕获异常")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
