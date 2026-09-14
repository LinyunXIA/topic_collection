"""F32 资讯→选题编排与 CLI：tc-extract（DESIGN §25.6/§25.7）。"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from feedkicker import bitable_lark, extract_llm, extract_source, extract_write, store
from feedkicker.config import PROJECT_ROOT, load_config
from feedkicker.config_models import Config
from feedkicker.extract_report import print_dry_run as print_dry_run
from feedkicker.extract_report import print_summary as print_summary

log = logging.getLogger(__name__)


def _token_missing(value: str) -> bool:
    return not value or "<" in value


def _positive_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"必须是正整数: {value}") from None
    if n < 1:
        raise argparse.ArgumentTypeError(f"必须是正整数: {value}")
    return n


def _non_negative_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"必须是非负整数: {value}") from None
    if n < 0:
        raise argparse.ArgumentTypeError(f"必须是非负整数: {value}")
    return n


def _refine_batches(
    ex, template: str, batches: list[list[dict[str, Any]]], max_calls: int
) -> tuple[list[dict[str, Any]], int, int]:
    collected: list[dict[str, Any]] = []
    calls = 0
    failed = 0
    for no, batch in enumerate(batches, 1):
        prompt = extract_llm.build_batch_prompt(template, batch)
        raw = ""
        limit_reached = False
        for attempt in (1, 2):
            if max_calls and calls >= max_calls:
                log.warning("达到 max_calls=%d 上限，停止剩余批", max_calls)
                limit_reached = True
                break
            calls += 1
            try:
                raw = extract_llm.call_llm(ex, prompt)
                break
            except Exception as e:  # noqa: BLE001
                log.warning("第 %d/%d 批 LLM 调用失败（attempt %d/2）: %s", no, len(batches), attempt, e)
        if limit_reached:
            break
        topics = extract_llm.parse_topics(raw) if raw else []
        if not topics:
            failed += 1
            log.warning("第 %d/%d 批无有效话题（解析失败或模型未产出），跳过", no, len(batches))
            continue
        collected.extend(topics)
        log.info("第 %d/%d 批提炼 %d 个话题", no, len(batches), len(topics))
    return collected, calls, failed


def run(
    cfg: Config,
    conn,
    *,
    apply: bool,
    since_days: int | None = None,
    limit: int | None = None,
    batch_size: int | None = None,
    max_calls: int | None = None,
) -> int:
    """选源→分批→LLM→解析→合并去重→dry-run 打印 / --apply 写入；rc 2 配置错 / 0 正常。"""
    ex = cfg.extract
    since_days = ex.since_days if since_days is None else since_days
    batch_size = ex.batch_size if batch_size is None else batch_size
    max_calls = ex.max_calls if max_calls is None else max_calls
    if since_days < 1 or batch_size < 1 or max_calls < 0:
        log.error("参数非法：since_days=%s batch_size=%s max_calls=%s", since_days, batch_size, max_calls)
        return 2
    if not ex.enabled:
        log.warning("extract.enabled=false；显式 CLI 调用仍执行")
    if _token_missing(cfg.salon.app_token) or _token_missing(cfg.salon.table_id):
        log.error("salon app_token/table_id 缺失或为占位，无法校验/写入选题表")
        return 2
    prompt_path = Path(ex.prompt_file)
    if not prompt_path.is_absolute():
        prompt_path = PROJECT_ROOT / prompt_path
    if not prompt_path.is_file():
        log.error("提示词文件不存在: %s", prompt_path)
        return 2
    try:
        provider = extract_llm.resolve_provider(ex)
    except RuntimeError as e:
        log.error("provider 配置错误: %s", e)
        return 2
    template = prompt_path.read_text(encoding="utf-8")
    run_date = datetime.now(bitable_lark.SHANGHAI).strftime("%Y-%m-%d")
    items = extract_source.select_source(conn, since_days, limit)
    log.info(
        "近 %d 天 RSS 行 %d 条（limit=%s）→ 批大小 %d，provider=%s",
        since_days, len(items), limit, batch_size, ex.provider,
    )
    batches = [items[i : i + batch_size] for i in range(0, len(items), batch_size)]
    collected, calls, failed = _refine_batches(ex, template, batches, max_calls)
    merged = extract_llm.merge_topics(collected)
    if apply:
        written, skipped = extract_write.write_topics(
            cfg.salon.app_token, cfg.salon.table_id, merged, provider.tool_label, run_date
        )
    else:
        written, skipped = extract_write.write_topics(
            cfg.salon.app_token, cfg.salon.table_id, merged, provider.tool_label, run_date, dry_run=True
        )
        print_dry_run(merged, provider.tool_label, run_date, skipped)
    print_summary(
        {
            "mode": "apply" if apply else "dry-run",
            "since_days": since_days,
            "batches": len(batches),
            "llm_calls": calls,
            "topics": len(merged),
            "written": written if apply else 0,
            "pending": 0 if apply else written,
            "skipped": skipped,
            "failed_batches": failed,
        }
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tc-extract",
        description="近 N 天 RSS 资讯 → LLM 提炼选题 → salon 选题表（默认 dry-run）",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="写入多维表格（默认 dry-run 只打印清单）")
    mode.add_argument("--dry-run", action="store_true", help="仅打印待写清单（默认行为）")
    parser.add_argument("--since-days", type=_positive_int, default=None, help="时间窗天数，默认取 extract.since_days")
    parser.add_argument("--limit", type=_positive_int, default=None, help="最多处理 RSS 行数，默认不限")
    parser.add_argument("--batch-size", type=_positive_int, default=None, help="每批条数，默认取 extract.batch_size")
    parser.add_argument(
        "--max-calls", type=_non_negative_int, default=None, help="LLM 调用上限（0=不限），默认取 extract.max_calls"
    )
    parser.add_argument("--config", default=None, help="指定 config-{env}.yaml 路径")
    parser.add_argument("--db", default=None, help="sqlite 路径（覆盖 TC_DB 与 --env 推导）")
    parser.add_argument("--env", default=None, choices=["dev", "test", "prod"], help="运行环境，决定默认配置文件与 db 路径")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        cfg = load_config(args.config, args.db, app_env=args.env)
    except Exception as e:  # noqa: BLE001
        log.error("%s", e)
        return 2

    conn = store.connect(cfg.db_path)
    log.info("tc-extract 运行开始：环境=%s，db=%s，mode=%s", cfg.app_env, cfg.db_path, "apply" if args.apply else "dry-run")
    try:
        return run(
            cfg,
            conn,
            apply=args.apply,
            since_days=args.since_days,
            limit=args.limit,
            batch_size=args.batch_size,
            max_calls=args.max_calls,
        )
    except Exception:  # noqa: BLE001
        log.exception("未捕获异常")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
