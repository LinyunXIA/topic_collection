from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from feedkicker.fetch import canonicalize, utc_now_iso

try:
    SHANGHAI = ZoneInfo("Asia/Shanghai")
except (ValueError, OSError):
    SHANGHAI = timezone(timedelta(hours=8))

log = logging.getLogger(__name__)

BASE_TITLE = "AI 资讯归档"
BASE_TITLE_DEV_TEST = "AI 资讯归档 · dev-test"
BASE_TITLES = {"prod": BASE_TITLE, "dev": BASE_TITLE_DEV_TEST, "test": BASE_TITLE_DEV_TEST}
BASE_TITLE_DEFAULT = BASE_TITLE
TABLE_NAME = "文章"
VIEW_NAME = "表格"
_CHUNK = 200

_LARK_CANDIDATES = ("/opt/homebrew/bin/lark-cli", "/usr/local/bin/lark-cli")

_FIELDS = [
    {"name": "标题", "type": "text"},
    {"name": "链接", "type": "url"},
    {"name": "来源", "type": "text"},
    {"name": "摘要", "type": "text"},
    {"name": "发布时间", "type": "datetime"},
    {"name": "推送时间", "type": "datetime"},
    {"name": "归档日期", "type": "text"},
]


def fields_for(app_env: str) -> list[dict]:
    if app_env in ("dev", "test"):
        return [_FIELDS[0]] + [{"name": "环境", "type": "text"}] + _FIELDS[1:]
    return list(_FIELDS)


def lark_bin() -> str:
    found = shutil.which("lark-cli")
    if found:
        return found
    for p in _LARK_CANDIDATES:
        if shutil.which(p):
            return p
    raise FileNotFoundError("找不到 lark-cli，请先安装 @larksuite/cli 并完成 auth login")


def _run(args: list[str], stdin_text: str | None = None, timeout: float = 120):
    cmd = [lark_bin()] + args
    try:
        proc = subprocess.run(
            cmd, input=stdin_text, capture_output=True, text=True, timeout=timeout
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("lark-cli 执行异常: %s", e)
        return None
    if proc is not None and proc.returncode != 0:
        log.warning("lark-cli 失败(%d): %s", proc.returncode, proc.stderr.strip()[:300])
    return proc


def _ok(proc) -> bool:
    return proc is not None and proc.returncode == 0


def _data(proc) -> dict:
    try:
        out = json.loads(proc.stdout or "{}")
        return out.get("data") or {}
    except json.JSONDecodeError:
        return {}


def base_url(app_token: str) -> str:
    return f"https://web91vfvm7.feishu.cn/base/{app_token}"


def find_base_by_title(title: str) -> dict | None:
    proc = _run(["base", "+title-resolve", "--title", title[:30]])
    if not _ok(proc):
        return None
    d = _data(proc)
    for cand in (
        d.get("bases"),
        d.get("items"),
        d.get("files") or d.get("docs"),
    ):
        if isinstance(cand, list):
            for b in cand:
                tok = b.get("base_token") or b.get("token") or ""
                if tok and (b.get("name") == title or title in str(b.get("title", ""))):
                    return {"app_token": tok, "url": b.get("url") or base_url(tok)}
    if isinstance(d.get("token"), str) and d["token"]:
        return {"app_token": d["token"], "url": d.get("url") or base_url(d["token"])}
    return None


def create_base(title: str, app_env: str = "prod") -> dict:
    proc = _run(
        [
            "base",
            "+base-create",
            "--name",
            title,
            "--table-name",
            TABLE_NAME,
            "--fields",
            json.dumps(fields_for(app_env), ensure_ascii=False),
            "--time-zone",
            "Asia/Shanghai",
        ],
        timeout=120,
    )
    if not _ok(proc):
        raise RuntimeError(f"创建 Base 失败: {proc.stderr[:200] if proc else 'unknown'}")
    d = _data(proc)
    base = d.get("base") or {}
    app_token = base.get("base_token") or ""
    if not app_token:
        raise RuntimeError(f"Base 创建响应缺少 token: {str(d)[:200]}")
    return {"app_token": app_token, "url": base.get("url") or base_url(app_token)}


def get_table_id(app_token: str) -> str | None:
    proc = _run(["base", "+table-list", "--base-token", app_token])
    if not _ok(proc):
        return None
    for t in _data(proc).get("tables") or []:
        if t.get("name") == TABLE_NAME:
            return t.get("id")
    return None


def create_table(app_token: str, app_env: str = "prod") -> str:
    proc = _run(
        [
            "base",
            "+table-create",
            "--base-token",
            app_token,
            "--name",
            TABLE_NAME,
            "--fields",
            json.dumps(fields_for(app_env), ensure_ascii=False),
        ],
        timeout=120,
    )
    if not _ok(proc):
        raise RuntimeError("创建数据表失败")
    table_id = (_data(proc).get("table_id"))
    return table_id or TABLE_NAME


def _view_id(app_token: str, table_id: str) -> str | None:
    proc = _run(["base", "+view-list", "--base-token", app_token, "--table-id", table_id])
    if not _ok(proc):
        return None
    views = _data(proc).get("views") or []
    for v in views:
        if v.get("view_name") == VIEW_NAME or v.get("name") == VIEW_NAME:
            return v.get("id")
    return views[0].get("id") if views else None


def setup_view(app_token: str, table_id: str) -> bool:
    vid = _view_id(app_token, table_id)
    if not vid:
        log.warning("未找到默认视图，跳过分组设置")
        return False
    g = _run(
        [
            "base", "+view-set-group",
            "--base-token", app_token,
            "--table-id", table_id,
            "--view-id", vid,
            "--json", json.dumps({"group_config": [{"field": "来源", "desc": False}]}, ensure_ascii=False),
        ]
    )
    s = _run(
        [
            "base", "+view-set-sort",
            "--base-token", app_token,
            "--table-id", table_id,
            "--view-id", vid,
            "--json", json.dumps({"sort_config": [{"field": "发布时间", "desc": True}]}, ensure_ascii=False),
        ]
    )
    return _ok(g) and _ok(s)


def set_tenant_readonly(app_token: str) -> bool:
    r1 = _run(
        ["drive", "permission.public", "patch", "--token", app_token,
         "--type", "bitable", "--data", '{"external_access": false}', "--yes"]
    )
    r2 = _run(
        ["drive", "permission.public", "patch", "--token", app_token,
         "--type", "bitable", "--data", '{"link_share_entity": "tenant_readable"}', "--yes"]
    )
    return _ok(r1) and _ok(r2)


def ensure_archive_date_field(app_token: str, table_id: str) -> bool:
    proc = _run(["base", "+field-list", "--base-token", app_token, "--table-id", table_id])
    names = set()
    if _ok(proc):
        d = _data(proc)
        items = d.get("fields") or d.get("items") or []
        for f in items:
            names.add(f.get("field_name") or f.get("name"))
    if "归档日期" in names:
        return True
    return _ok(_run(
        ["base", "+field-create", "--base-token", app_token, "--table-id", table_id,
         "--json", '{"name":"归档日期","type":"text"}'],
        timeout=60,
    ))


def create_date_view(app_token: str, table_id: str) -> bool:
    proc = _run(
        ["base", "+view-create", "--base-token", app_token,
         "--table-id", table_id,
         "--json", json.dumps({"name": "按日期", "type": "grid"}, ensure_ascii=False)],
        timeout=60,
    )
    if not _ok(proc):
        return False
    vid = None
    d = _data(proc)
    v = (d.get("view") or {})
    vid = v.get("view_id") or v.get("id")
    if not vid:
        vid = _view_id(app_token, table_id)
    if not vid:
        return False
    g = _run(
        ["base", "+view-set-group", "--base-token", app_token,
         "--table-id", table_id, "--view-id", vid,
         "--json", json.dumps({"group_config": [{"field": "归档日期", "desc": True}]},
                              ensure_ascii=False)],
    )
    s = _run(
        ["base", "+view-set-sort", "--base-token", app_token,
         "--table-id", table_id, "--view-id", vid,
         "--json", json.dumps({"sort_config": [{"field": "推送时间", "desc": True}]},
                              ensure_ascii=False)],
    )
    return _ok(g) and _ok(s)


def _markdown_record_ids(stdout: str) -> list[str]:
    ids = []
    for line in stdout.splitlines():
        m = re.match(r"^\|\s*(rec[A-Za-z0-9]+)\s*\|", line)
        if m:
            ids.append(m.group(1))
    return ids


def purge_all_records(app_token: str, table_id: str) -> int:
    """清空数据表全部记录（用于结构变更后的干净重灌）。返回删除数。"""
    deleted = 0
    while True:
        proc = _run(
            ["base", "+record-list", "--base-token", app_token,
             "--table-id", table_id, "--limit", "200",
             "--format", "markdown"],
            timeout=120,
        )
        if not _ok(proc):
            break
        ids = _markdown_record_ids(proc.stdout or "")
        if not ids:
            break
        d = _run(["base", "+record-delete", "--base-token", app_token,
                  "--table-id", table_id,
                  "--json", json.dumps({"record_id_list": ids}, ensure_ascii=False),
                  "--yes"], timeout=300)
        if not _ok(d):
            log.warning("批量删除失败，终止清空")
            break
        deleted += len(ids)
        if len(ids) < 200:
            break
    return deleted


def ensure_initialized(bt, app_env: str = "prod") -> dict:
    title = BASE_TITLES.get(app_env, BASE_TITLE_DEFAULT)
    info: dict = {"app_token": bt.app_token, "table_id": bt.table_id,
                  "url": bt.url or (base_url(bt.app_token) if bt.app_token else "")}
    if not info["app_token"]:
        found = find_base_by_title(title)
        if found is None:
            found = create_base(title, app_env)
        info["app_token"] = found["app_token"]
        info.setdefault("url", found.get("url") or base_url(info["app_token"]))
    if not info["table_id"]:
        info["table_id"] = get_table_id(info["app_token"]) or create_table(info["app_token"], app_env)
    return info


def _cell(item: dict, now_iso: str | None = None, env_name: str | None = None) -> dict:
    if env_name is None and now_iso in ("dev", "test"):
        env_name = now_iso
        now_iso = None

    def fmt_dt(iso: str | None) -> str | None:
        if not iso:
            return None
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(SHANGHAI)
        except ValueError:
            return None
        return dt.strftime("%Y-%m-%d %H:%M")

    cell = {
        "标题": item.get("title") or item.get("url") or "",
        "链接": item.get("url") or "",
        "来源": item.get("feed_id") or "",
        "摘要": item.get("description") or "",
        "发布时间": fmt_dt(item.get("published_at")),
        "推送时间": fmt_dt(item.get("pushed_at")),
        "归档日期": (fmt_dt(item.get("pushed_at")) or fmt_dt(now_iso) or fmt_dt(item.get("first_seen")) or "")[:10],
    }
    if env_name:
        cell["环境"] = env_name
    return cell


def existing_links(app_token: str, table_id: str) -> set[str]:
    links: set[str] = set()
    offset = 0
    while True:
        proc = _run(
            [
                "base", "+record-list",
                "--base-token", app_token,
                "--table-id", table_id,
                "--field-id", "链接",
                "--limit", "200",
                "--offset", str(offset),
                "--json",
            ],
            timeout=120,
        )
        if not _ok(proc):
            break
        data = _data(proc)
        fields = data.get("fields") or []
        if "链接" not in fields:
            break
        i_link = fields.index("链接")
        rows = data.get("data") or []
        for r in rows:
            v = r[i_link]
            v = v.get("link") if isinstance(v, dict) else v
            if v:
                links.add(canonicalize(v))
        if len(rows) < 200:
            break
        offset += 200
    return links


def sync_records(app_token: str, table_id: str, items: list[dict], env_name: str | None = None, now_iso: str | None = None) -> bool:
    if not items:
        return True
    if now_iso is None:
        now_iso = utc_now_iso()
    seen_links = existing_links(app_token, table_id)
    picked: list[dict] = []
    batch_seen: set[str] = set()
    skipped = 0
    for it in items:
        key = canonicalize(it.get("url") or "")
        if key and (key in seen_links or key in batch_seen):
            skipped += 1
            continue
        if key:
            batch_seen.add(key)
        picked.append(it)
    if skipped:
        log.info("跨源/已存在去重跳过 %d 条", skipped)
    if not picked:
        log.info("全部条目已存在于表内，无需写入")
        return True
    total_ok = True
    for i in range(0, len(picked), _CHUNK):
        chunk = picked[i : i + _CHUNK]
        payload = json.dumps(
            {"create_records": [_cell(it, now_iso, env_name) for it in chunk]}, ensure_ascii=False
        )
        proc = _run(
            [
                "base", "+record-batch-create",
                "--base-token", app_token,
                "--table-id", table_id,
                "--json", payload,
            ],
            timeout=300,
        )
        if not _ok(proc):
            total_ok = False
            log.warning("Bitable 批量写入失败（第 %d 批 %d 条）", i // _CHUNK + 1, len(chunk))
    return total_ok


def _has_batch_verb() -> str | None:
    proc = _run(["base", "--help"])
    txt = proc.stdout if proc and proc.stdout else ""
    if "+record-batch-update" in txt:
        return "+record-batch-update"
    if "+record-update" in txt:
        return "+record-update"
    return None


def _cell_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, dict):
        for k in ("link", "text", "value", "title"):
            if k in v and isinstance(v[k], str):
                return v[k]
            if k in v and v[k] is not None:
                return str(v[k])
        vals = [str(x) for x in v.values() if isinstance(x, str) and x]
        return vals[0] if vals else ""
    if isinstance(v, list):
        if v and isinstance(v[0], str):
            return v[0]
        if v and isinstance(v[0], dict):
            for k in ("link", "text", "value"):
                if k in v[0]:
                    return str(v[0][k])
        return ""
    return str(v)


def _shanghai_date(s: str) -> str | None:
    if not s or not s.strip():
        return None
    s = s.strip()
    if s.isdigit():
        try:
            iv = int(s)
            if iv > 1_000_000_000_000:
                iv = iv // 1000
            dt = datetime.fromtimestamp(iv, tz=UTC).astimezone(SHANGHAI)
            return dt.strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError):
            return None
    try:
        if "T" in s or s.endswith("Z") or "+" in s[10:]:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=SHANGHAI)
            else:
                dt = dt.astimezone(SHANGHAI)
            return dt.strftime("%Y-%m-%d")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s, fmt)
                dt = dt.replace(tzinfo=SHANGHAI)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=SHANGHAI)
        else:
            dt = dt.astimezone(SHANGHAI)
        return dt.strftime("%Y-%m-%d")
    except (ValueError, OSError, OverflowError):
        return None


def backfill_empty_archive_dates(app_token: str, table_id: str, env_name: str | None = None, dry_run: bool = False) -> int:
    verb = _has_batch_verb()
    if not verb:
        log.warning("lark-cli 未提供批量更新 verb（缺 +record-batch-update/+record-update），请改用 --reseed 重灌")
        return 0
    offset = 0
    to_fix: list[tuple[str, str]] = []
    total_scanned = 0
    while True:
        proc = _run(
            [
                "base", "+record-list",
                "--base-token", app_token,
                "--table-id", table_id,
                "--limit", "200",
                "--offset", str(offset),
                "--json",
            ],
            timeout=120,
        )
        if not _ok(proc):
            break
        data = _data(proc)
        fields: list[str] = data.get("fields") or []
        rows: list = data.get("data") or []
        records: list[dict] = data.get("records") or []
        if records:
            for rec in records:
                total_scanned += 1
                rid = rec.get("record_id") or rec.get("id") or rec.get("recordId") or ""
                fds = rec.get("fields") or rec.get("record") or {}
                arch = _cell_str(fds.get("归档日期"))
                if arch.strip():
                    continue
                cand = ""
                for key in ("推送时间", "bitable_synced_at", "first_seen"):
                    v = fds.get(key)
                    if v is not None:
                        cand = _cell_str(v)
                        if cand.strip():
                            break
                d = _shanghai_date(cand) if cand else None
                if not d:
                    continue
                if rid:
                    to_fix.append((rid, d))
            if len(records) < 200:
                break
            offset += 200
            continue
        if not fields or not rows:
            break
        idx_arch = fields.index("归档日期") if "归档日期" in fields else -1
        idx_push = fields.index("推送时间") if "推送时间" in fields else -1
        idx_synced = fields.index("bitable_synced_at") if "bitable_synced_at" in fields else -1
        idx_first = fields.index("first_seen") if "first_seen" in fields else -1
        rids = data.get("record_ids") or data.get("recordIds") or data.get("ids") or []
        for i, r in enumerate(rows):
            total_scanned += 1
            if isinstance(r, dict):
                rid = r.get("record_id") or r.get("id") or (rids[i] if i < len(rids) else "")
                vals = r.get("fields") or r.get("values") or r
                if isinstance(vals, dict):
                    arch = _cell_str(vals.get("归档日期"))
                    if arch.strip():
                        continue
                    cand = ""
                    for key in ("推送时间", "bitable_synced_at", "first_seen"):
                        vv = vals.get(key)
                        if vv is not None:
                            cand = _cell_str(vv)
                            if cand.strip():
                                break
                else:
                    arch = _cell_str(r[idx_arch]) if idx_arch >= 0 and idx_arch < len(r) else ""
                    if arch.strip():
                        continue
                    cand = ""
                    for idx in (idx_push, idx_synced, idx_first):
                        if idx >= 0 and idx < len(r):
                            cc = _cell_str(r[idx])
                            if cc.strip():
                                cand = cc
                                break
                d = _shanghai_date(cand) if cand else None
                if not d or not rid:
                    continue
                to_fix.append((rid, d))
                continue
            if not isinstance(r, list):
                continue
            rid = rids[i] if i < len(rids) else ""
            if not rid and idx_arch == -1:
                continue
            arch = _cell_str(r[idx_arch]) if idx_arch >= 0 and idx_arch < len(r) else ""
            if arch.strip():
                continue
            cand = ""
            for idx in (idx_push, idx_synced, idx_first):
                if idx >= 0 and idx < len(r):
                    cc = _cell_str(r[idx])
                    if cc.strip():
                        cand = cc
                        break
            d = _shanghai_date(cand) if cand else None
            if not d or not rid:
                continue
            to_fix.append((rid, d))
        if len(rows) < 200:
            break
        offset += 200
    if dry_run:
        log.info("backfill dry-run：扫描 %d 条，待修复 %d 条（未写入）", total_scanned, len(to_fix))
        return len(to_fix)
    fixed = 0
    for i in range(0, len(to_fix), _CHUNK):
        chunk = to_fix[i : i + _CHUNK]
        if verb == "+record-batch-update":
            payload = json.dumps({"update_records": {rid: {"归档日期": d} for rid, d in chunk}}, ensure_ascii=False)
            proc = _run(
                ["base", verb, "--base-token", app_token, "--table-id", table_id, "--json", payload],
                timeout=300,
            )
            if _ok(proc):
                fixed += len(chunk)
            else:
                log.warning("Bitable 批量回填失败（第 %d 批 %d 条）", i // _CHUNK + 1, len(chunk))
        else:
            for rid, d in chunk:
                payload = json.dumps({"record_id": rid, "fields": {"归档日期": d}}, ensure_ascii=False)
                proc = _run(
                    ["base", verb, "--base-token", app_token, "--table-id", table_id, "--json", payload],
                    timeout=60,
                )
                if _ok(proc):
                    fixed += 1
                else:
                    log.warning("Bitable 单条回填失败 %s", rid)
    log.info("backfill 完成：扫描 %d 条，修复 %d 条", total_scanned, fixed)
    return fixed


def sync_env(bt, app_env: str, conn, now_iso=None) -> int:
    from feedkicker import store

    info = ensure_initialized(bt, app_env)
    if not info["app_token"] or not info["table_id"]:
        raise RuntimeError(f"Base 初始化不完整: {info}")
    unsynced = store.select_unsynced(conn)
    log.info("多维表格待同步 %d 条 → %s", len(unsynced), info["url"])
    if not unsynced:
        return 0
    env_name = app_env if app_env in ("dev", "test") else None
    ok = sync_records(info["app_token"], info["table_id"], unsynced, env_name=env_name, now_iso=now_iso)
    if not ok:
        raise RuntimeError("存在未成功的批次，保留待重试")
    store.mark_synced(conn, unsynced, now_iso or utc_now_iso())
    return len(unsynced)


if __name__ == "__main__":
    import argparse

    from feedkicker import store
    from feedkicker.config import load_config

    parser = argparse.ArgumentParser(prog="tc-bitable")
    parser.add_argument("--env", default=None, choices=["dev", "test", "prod"])
    parser.add_argument("--init", action="store_true", help="补字段/视图/组织内只读分享")
    parser.add_argument("--reseed", action="store_true", help="清空表内记录后全量重灌")
    parser.add_argument("--backfill", action="store_true", help="回填存量空归档日期")
    parser.add_argument("--fix-archive-date", action="store_true", help="回填存量空归档日期（--backfill 别名）")
    parser.add_argument("--dry-run", action="store_true", help="仅统计待修复数，不写回")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(app_env=args.env)
    if not cfg.bitable.enabled:
        log.info("bitable 未启用（%s）", cfg.app_env)
        raise SystemExit(0)

    info = ensure_initialized(cfg.bitable, cfg.app_env)
    log.info("Base: %s", info["url"])

    if args.init:
        ensure_archive_date_field(info["app_token"], info["table_id"])
        if setup_view(info["app_token"], info["table_id"]):
            log.info("「按来源」分组视图已设置")
        if create_date_view(info["app_token"], info["table_id"]):
            log.info("「按日期」分组视图已创建")
        if set_tenant_readonly(info["app_token"]):
            log.info("分享已设为组织内只读")
        print(json.dumps(info, ensure_ascii=False))

    conn = store.connect(cfg.db_path)
    try:
        if args.reseed:
            n = purge_all_records(info["app_token"], info["table_id"])
            log.info("已清空 %d 条旧记录，准备重灌", n)
        if args.backfill or args.fix_archive_date:
            env_name = cfg.app_env if cfg.app_env in ("dev", "test") else None
            n = backfill_empty_archive_dates(info["app_token"], info["table_id"], env_name=env_name, dry_run=args.dry_run)
            log.info("归档日期回填：%d 条", n)
        synced = sync_env(cfg.bitable, cfg.app_env, conn)
        log.info("同步完成：%d 条", synced)
    finally:
        conn.close()
