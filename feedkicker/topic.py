from __future__ import annotations

import argparse
import json
import logging
from typing import Any

from feedkicker import bitable_lark
from feedkicker.log_setup import PLAIN_FORMAT, setup_logging
from feedkicker.topic_records import _extract_records as _extract_records

log = logging.getLogger(__name__)

FILTER_JSON = json.dumps({"logic": "and", "conditions": [["讨论状态", "intersects", ["已选题"]]]}, ensure_ascii=False)


def _has_more_of(data: dict[str, Any]) -> bool | None:
    """has_more 归一：字符串真值含 "true"/"1"；`has_more: null` 回退看 `hasMore`（#277）。"""
    raw = data.get("has_more")
    if raw is None:
        raw = data.get("hasMore")
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw.strip().lower() in ("true", "1")
    return bool(raw)


def _positive_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"必须是正整数: {value}") from None
    if n < 1:
        raise argparse.ArgumentTypeError(f"必须是正整数: {value}")
    return n


def fetch_selected_topics(
    app_token: str, table_id: str, limit: int = 200
) -> list[dict[str, Any]]:
    """拉取讨论状态 intersects 已选题 的记录，自动分页。

    响应兼容 records/items 包装与 data.fields+data.data 行式两种形态；
    空页且 has_more 非真即终止，has_more 缺失时以不足一页判定结束；
    翻页走页指纹守卫（#245）+ 页数上限（按**实际页计数**，与 --limit 一致，#348）与 offset
    双兜底；空页计数按**本轮累计**，交替空页同样在有限页内熔断（#348）；limit 上界 200（#348）。
    """
    if not app_token or not table_id:
        raise ValueError("app_token 与 table_id 均不能为空")
    limit = min(max(1, limit), 200)
    all_records: list[dict[str, Any]] = []
    offset = 0
    prev_fp = ""
    pages = 0
    empty_pages = 0
    while True:
        pages += 1
        bitable_lark._guard_offset(offset)
        bitable_lark.guard_pages(pages)
        args = [
            "base", "+record-list",
            "--base-token", app_token,
            "--table-id", table_id,
            "--filter-json", FILTER_JSON,
            "--limit", str(limit),
            "--offset", str(offset),
            "--json",
        ]
        proc = bitable_lark._run(args, timeout=120)
        if proc is None:
            raise RuntimeError("lark-cli 执行失败(无返回)，疑似超时或未安装")
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout or "").strip()[:500]
            log.warning("lark-cli 失败(%d): %s", proc.returncode, msg)
            raise RuntimeError(f"lark-cli 失败({proc.returncode}): {msg}")
        ok, data = bitable_lark._parse(proc)
        if not ok:
            msg = (proc.stdout or proc.stderr or "").strip()[:500]
            log.warning("lark-cli 业务失败: %s", msg)
            raise RuntimeError(f"lark-cli 业务失败: {msg}")
        prev_fp = bitable_lark._page_guard(prev_fp, data)
        chunk = _extract_records(data)
        has_more = _has_more_of(data)
        if not chunk:
            if has_more is True:
                empty_pages += 1
                if empty_pages >= 2:
                    raise RuntimeError("分页连续空页但 has_more 恒真，疑似分页异常，中止以避免死循环")
                offset += limit
                continue
            break
        all_records.extend(chunk)
        if has_more is not None:
            if not has_more:
                break
            offset += len(chunk)
            continue
        if len(chunk) < limit:
            break
        offset += len(chunk)
    return all_records


def fetch_topic_fields(app_token: str, table_id: str) -> list[dict[str, Any]]:
    """列出表字段并校验 讨论状态；select/singleSelect/multiSelect 均视为合法（intersects 已验证可用）。

    `fields`/`items` 非 list[dict]（str / list[str] / dict 等）一律抛 RuntimeError，
    否则迭代时 `f.get` 会裸 AttributeError（#244 只收口了 records 容器，#276）。
    """
    if not app_token or not table_id:
        raise ValueError("app_token 与 table_id 均不能为空")
    proc = bitable_lark._run(["base", "+field-list", "--base-token", app_token, "--table-id", table_id], timeout=60)
    if proc is None:
        raise RuntimeError("lark-cli 执行失败(无返回)")
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()[:500]
        log.warning("lark-cli 失败(%d): %s", proc.returncode, msg)
        raise RuntimeError(f"lark-cli 失败({proc.returncode}): {msg}")
    ok, data = bitable_lark._parse(proc)
    if not ok:
        msg = (proc.stdout or proc.stderr or "").strip()[:500]
        log.warning("lark-cli 业务失败: %s", msg)
        raise RuntimeError(f"lark-cli 业务失败: {msg}")
    fields_raw = data.get("fields") or data.get("items") or []
    if not isinstance(fields_raw, list) or not all(isinstance(f, dict) for f in fields_raw):
        raise RuntimeError(
            f"field-list 响应 fields 非 list[dict]: {type(fields_raw).__name__}: {str(fields_raw)[:200]}"
        )
    fields: list[dict[str, Any]] = fields_raw
    found = None
    for f in fields:
        name = f.get("field_name") or f.get("name") or ""
        if name == "讨论状态":
            found = f
            break
    if found is None:
        log.warning("未找到 讨论状态 字段")
    else:
        ftype = str(found.get("type") or found.get("field_type") or "").lower()
        if ftype and ftype not in ("select", "singleselect", "multiselect", "single_select", "multiple_select", "7", "3", "4"):
            log.warning("讨论状态字段类型异常: %s", ftype)
    return fields


def _stub_out(check_fields: bool) -> int:
    if check_fields:
        log.warning("--check-fields 被跳过：app_token/table_id 缺失，仅打印 stub")
    stub = [{"record_id": "recStub000", "fields": {"讨论状态": ["已选题"], "话题名称": "示例已选题话题"}}]
    print(json.dumps(stub, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="feedkicker.topic")
    parser.add_argument("--env", default=None, choices=["dev", "test", "prod"], help="环境名，对应 config-{env}.yaml")
    parser.add_argument("--app-token", default=None, help="覆盖多维表 app_token")
    parser.add_argument("--table-id", default=None, help="覆盖表 id")
    parser.add_argument("--limit", type=_positive_int, default=200, help="分页大小（正整数）")
    parser.add_argument("--dry-run", action="store_true", help="仅打印，不校验远端副作用")
    parser.add_argument("--check-fields", action="store_true", help="校验 讨论状态 字段类型")
    args = parser.parse_args(argv)

    setup_logging(logging.INFO, PLAIN_FORMAT)

    app_token, table_id = args.app_token, args.table_id
    if not app_token or not table_id:
        try:
            from feedkicker.config import load_config

            cfg = load_config(app_env=args.env)
            if not app_token:
                app_token = cfg.salon.app_token
            if not table_id:
                table_id = cfg.salon.table_id
            if (not app_token or not table_id) and args.dry_run:
                return _stub_out(args.check_fields)
        except Exception:  # noqa: BLE001
            if args.dry_run:
                return _stub_out(args.check_fields)
            raise

    if args.check_fields:
        fields = fetch_topic_fields(app_token, table_id)
        print(json.dumps(fields, ensure_ascii=False, indent=2))
    elif args.dry_run and (not app_token or not table_id):
        _stub_out(args.check_fields)
    else:
        records = fetch_selected_topics(app_token, table_id, limit=args.limit)
        print(json.dumps(records, ensure_ascii=False, indent=2))
        log.info("已选题 %d 条", len(records))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
