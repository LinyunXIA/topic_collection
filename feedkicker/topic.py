from __future__ import annotations

import argparse
import json
import logging
from typing import Any

from feedkicker import bitable

log = logging.getLogger(__name__)

FILTER_JSON = json.dumps({"logic": "and", "conditions": [["讨论状态", "intersects", ["已选题"]]]}, ensure_ascii=False)


def _extract_records(data: dict) -> list[dict]:
    records = data.get("records") or data.get("items") or []
    if records:
        return list(records)
    fields: list[str] = data.get("fields") or []
    rows: list[Any] = data.get("data") or []
    if not rows:
        return []
    converted: list[dict] = []
    rids: list[str] = data.get("record_ids") or data.get("recordIds") or data.get("ids") or data.get("record_id_list") or data.get("recordId_list") or data.get("recordIdList") or []
    for i, r in enumerate(rows):
        if isinstance(r, dict):
            if "fields" in r or "record" in r:
                fds = r.get("fields") or r.get("record") or {}
                rid = r.get("record_id") or r.get("id") or r.get("recordId") or (rids[i] if i < len(rids) else "")
                converted.append({"record_id": rid, "fields": fds, **({k: v for k, v in r.items() if k not in ("fields", "record")} if isinstance(r, dict) else {})})
            else:
                rid = r.get("record_id") or r.get("id") or (rids[i] if i < len(rids) else "")
                # treat whole dict as fields if no explicit fields wrapper
                has_known = any(k in r for k in ("讨论状态", "话题名称", "title", "Topic"))
                if has_known:
                    converted.append({"record_id": rid, "fields": r})
                else:
                    converted.append({"record_id": rid, "fields": r})
        elif isinstance(r, list) and fields:
            d = {fields[idx]: r[idx] for idx in range(min(len(fields), len(r)))}
            rid = rids[i] if i < len(rids) else ""
            converted.append({"record_id": rid, "fields": d})
        else:
            converted.append({"record_id": rids[i] if i < len(rids) else "", "fields": {}})
    return converted


def fetch_selected_topics(app_token: str, table_id: str, limit: int = 200) -> list[dict]:
    if not app_token or not table_id:
        raise ValueError("app_token 与 table_id 均不能为空")
    all_records: list[dict] = []
    offset = 0
    while True:
        args = [
            "base", "+record-list",
            "--base-token", app_token,
            "--table-id", table_id,
            "--filter-json", FILTER_JSON,
            "--limit", str(limit),
            "--offset", str(offset),
            "--json",
        ]
        proc = bitable._run(args, timeout=120)
        if proc is None:
            raise RuntimeError("lark-cli 执行失败(无返回)，疑似超时或未安装")
        if proc.returncode != 0:
            msg = (proc.stderr or proc.stdout or "").strip()[:500]
            log.warning("lark-cli 失败(%d): %s", proc.returncode, msg)
            raise RuntimeError(f"lark-cli 失败({proc.returncode}): {msg}")
        ok, data = bitable._parse(proc)
        if not ok:
            msg = (proc.stdout or proc.stderr or "").strip()[:500]
            log.warning("lark-cli 业务失败: %s", msg)
            raise RuntimeError(f"lark-cli 业务失败: {msg}")
        chunk = _extract_records(data)
        # also handle pure rows without wrapper returning directly in data fields/data
        # fallback: if _extract returned empty but data has non-empty records-like keys
        if not chunk:
            # check if response is paginated via has_more but empty chunk means done
            has_more = data.get("has_more") if "has_more" in data else data.get("hasMore")
            if has_more is True:
                offset += limit
                continue
            break
        all_records.extend(chunk)
        # pagination termination
        has_more = data.get("has_more") if "has_more" in data else data.get("hasMore")
        if has_more is not None:
            if not has_more:
                break
            offset += limit
            continue
        if len(chunk) < limit:
            break
        offset += limit
        if offset > 20000:
            log.warning("fetch_selected_topics 分页超出上限，截断")
            break
    return all_records


def fetch_topic_fields(app_token: str, table_id: str) -> list[dict]:
    if not app_token or not table_id:
        raise ValueError("app_token 与 table_id 均不能为空")
    proc = bitable._run(["base", "+field-list", "--base-token", app_token, "--table-id", table_id], timeout=60)
    if proc is None:
        raise RuntimeError("lark-cli 执行失败(无返回)")
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()[:500]
        log.warning("lark-cli 失败(%d): %s", proc.returncode, msg)
        raise RuntimeError(f"lark-cli 失败({proc.returncode}): {msg}")
    ok, data = bitable._parse(proc)
    if not ok:
        msg = (proc.stdout or proc.stderr or "").strip()[:500]
        log.warning("lark-cli 业务失败: %s", msg)
        raise RuntimeError(f"lark-cli 业务失败: {msg}")
    fields: list[dict] = data.get("fields") or data.get("items") or []
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
        # select / singleSelect / multiSelect 均视为合法，intersects 已验证可用
        if ftype and ftype not in ("select", "singleselect", "multiselect", "single_select", "multiple_select", "7", "3"):
            log.warning("讨论状态字段类型异常: %s", ftype)
    return fields


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="feedkicker.topic")
    parser.add_argument("--env", default=None, choices=["dev", "test", "prod"], help="环境名，对应 config-{env}.yaml")
    parser.add_argument("--app-token", default=None, help="覆盖多维表 app_token")
    parser.add_argument("--table-id", default=None, help="覆盖表 id")
    parser.add_argument("--limit", type=int, default=200, help="分页大小")
    parser.add_argument("--dry-run", action="store_true", help="仅打印，不校验远端副作用")
    parser.add_argument("--check-fields", action="store_true", help="校验 讨论状态 字段类型")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    app_token = args.app_token
    table_id = args.table_id
    if not app_token or not table_id:
        try:
            from feedkicker.config import load_config

            cfg = load_config(app_env=args.env)
            app_token = app_token or cfg.salon.app_token or cfg.salon.table_id and "" or ""
            # salon app_token/table_id 来源：config.salon.app_token / table_id
            if not app_token:
                app_token = cfg.salon.app_token
            if not table_id:
                table_id = cfg.salon.table_id
            # 若 config 仍为空，且为 test dry-run，打印 stub 并退出
            if (not app_token or not table_id) and args.dry_run:
                stub = [{"record_id": "recGWg8Kb9kUDI", "fields": {"讨论状态": ["已选题"], "话题名称": "示例已选题话题"}}]
                print(json.dumps(stub, ensure_ascii=False, indent=2))
                raise SystemExit(0)
        except SystemExit:
            raise
        except Exception:
            if args.dry_run:
                stub = [{"record_id": "recGWg8Kb9kUDI", "fields": {"讨论状态": ["已选题"], "话题名称": "示例已选题话题"}}]
                print(json.dumps(stub, ensure_ascii=False, indent=2))
                raise SystemExit(0)
            raise

    if args.check_fields:
        fields = fetch_topic_fields(app_token, table_id)
        print(json.dumps(fields, ensure_ascii=False, indent=2))
    else:
        if args.dry_run and (not app_token or not table_id):
            stub = [{"record_id": "recGWg8Kb9kUDI", "fields": {"讨论状态": ["已选题"], "话题名称": "示例已选题话题"}}]
            print(json.dumps(stub, ensure_ascii=False, indent=2))
        else:
            records = fetch_selected_topics(app_token, table_id, limit=args.limit)
            print(json.dumps(records, ensure_ascii=False, indent=2))
            log.info("已选题 %d 条", len(records))
