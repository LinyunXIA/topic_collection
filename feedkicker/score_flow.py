"""F38 tc-score CLI 骨架：读表 → 目标列校验 → 组批 → dry-run 计划（DESIGN §26.10）。"""

from __future__ import annotations

import argparse
import logging
import sys

from feedkicker import bitable_lark, score_llm, score_parse, score_report, score_source, score_write
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
    """读全表 → 校验目标列 → 组批 → 逐批 LLM 打分 → dry-run 清单 / `--apply` 写入。

    rc 2 配置/参数非法；prompt 文件缺失 / provider 缺 key / 目标列缺失均 rc2，且均在发起任何
    lark/LLM 调用前判定。默认只补空：已有 `打分`/`理由` 的行计入 skipped 并作为横向上文，
    不进入本批（§26.7）；`--apply` 全失败（written=0 且 failed_writes>0）→ rc1（§26.6）。
    """
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
    try:
        template = score_llm.load_template(cfg.score.prompt_file)
    except RuntimeError as e:
        log.error("提示词错误: %s", e)
        return 2
    try:
        provider_conf = score_llm.resolve_for_score(cfg.score, provider)
    except RuntimeError as e:
        log.error("provider 配置错误: %s", e)
        return 2
    app_token, table_id = cfg.salon.app_token, cfg.salon.table_id
    if ensure_columns(app_token, table_id, provider) != 0:
        return 2
    projection = (*score_source.SCORE_FIELDS, *PROVIDER_COLUMNS[provider])
    rows = score_source.read_rows(app_token, table_id, limit, projection)
    pending, skipped = score_source.plan_pending(rows, provider, force)
    batches = score_source.group_batches(pending, cfg.score.batch_size)
    sizes = [len(b) for b in batches]
    print(f"tc-score dry-run 计划：总行数={len(rows)} 批数={len(batches)} 待打分={len(pending)} 跳过={len(skipped)}")
    log.info(
        "tc-score 运行计划：环境=%s db=%s 目标表=%s 模板=%s provider=%s（%s）总行数=%d 批数=%d 每批行数=%s 待打分=%d 跳过=%d 横向上文=%d max_calls=%s force=%s",
        cfg.app_env, cfg.db_path, table_id, cfg.score.prompt_file, provider, provider_conf.tool_label,
        len(rows), len(batches), sizes, len(pending), len(skipped), len(skipped), max_calls, force,
    )
    prior_scores = [
        (str(row.get("话题名称") or ""), str(row.get("打分")))
        for row in skipped
        if row.get("打分") is not None and str(row.get("打分")).strip()
    ]
    result = score_llm.refine_batches(
        provider_conf, template, batches, prior_scores, max_calls, cfg.score.timeout_seconds
    )
    to_write, skipped_plan = score_write.plan_writes(result.scored, force=force)
    if not apply:
        score_report.print_dry_run(to_write, skipped, score_parse.check_distribution(result.scored))
    write_conf = score_write.WriteConf(app_token, table_id, provider)
    written_stats = score_write.write_scores(write_conf, to_write, dry_run=not apply)
    score_report.print_summary(
        {
            "mode": "apply" if apply else "dry-run",
            "batches": len(batches),
            "llm_calls": result.calls,
            "rows": len(rows),
            "scored": len(result.scored),
            "skipped": len(skipped) + len(skipped_plan),
            "written": written_stats.written,
            "failed_writes": written_stats.failed_writes,
            "failed_batches": result.failed,
            "dropped": result.dropped,
            "empty_batches": result.empty,
        }
    )
    if apply and written_stats.failed_writes and not written_stats.written:
        log.error("写入全部失败：failed_writes=%d（rc=1）", written_stats.failed_writes)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tc-score",
        description="沙龙话题清单自动打分（默认 dry-run，--apply 才写表）",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="写回打分/理由列（默认只补空，--force 覆盖重算）")
    mode.add_argument("--dry-run", action="store_true", help="仅打印计划（默认行为）")
    parser.add_argument(
        "--provider", choices=sorted(PROVIDER_COLUMNS), default=None,
        help="LLM provider（默认取 config score.provider，默认 minimax）",
    )
    parser.add_argument("--limit", type=_non_negative_int, default=0, help="最多处理行数（0=全部）")
    parser.add_argument("--max-calls", type=_non_negative_int, default=0, help="LLM 调用上限（0=不限）")
    parser.add_argument("--force", action="store_true", help="忽略既有打分重算（覆盖目标两列）")
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
