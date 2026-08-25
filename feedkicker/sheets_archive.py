from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta

from feedkicker.fetch import canonicalize, utc_now_iso

log = logging.getLogger(__name__)

_TITLES = {
    "prod": "AI 资讯归档",
    "dev": "AI 资讯归档 · dev-test",
    "test": "AI 资讯归档 · dev-test",
}
_HEADERS = ["环境", "来源", "标题", "链接", "摘要", "发布时间", "推送时间"]
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_LARK_CANDIDATES = ("/opt/homebrew/bin/lark-cli", "/usr/local/bin/lark-cli")


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
        return subprocess.run(
            cmd, input=stdin_text, capture_output=True, text=True, timeout=timeout
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("lark-cli 执行异常: %s", e)
        return None


def _ok(proc) -> bool:
    return proc is not None and proc.returncode == 0


def _data(proc) -> dict:
    try:
        out = json.loads(proc.stdout or "{}")
        return out.get("data") or {}
    except json.JSONDecodeError:
        return {}


def base_url(token: str) -> str:
    return f"https://web91vfvm7.feishu.cn/sheets/{token}"


def create_spreadsheet(title: str) -> dict:
    proc = _run(
        [
            "api", "POST", "/open-apis/sheets/v3/spreadsheets",
            "--data", json.dumps({"title": title}, ensure_ascii=False),
        ],
        timeout=120,
    )
    if not _ok(proc):
        raise RuntimeError(f"创建电子表格失败: {proc.stderr[:200] if proc else 'unknown'}")
    sp = (_data(proc).get("spreadsheet")) or {}
    token = sp.get("spreadsheet_token") or ""
    if not token:
        raise RuntimeError(f"创建响应缺少 token: {str(_data(proc))[:200]}")
    return {"spreadsheet_token": token, "url": sp.get("url") or base_url(token)}


def list_sheets(token: str) -> list[dict]:
    proc = _run(
        ["api", "GET", f"/open-apis/sheets/v3/spreadsheets/{token}/sheets/query"],
        timeout=60,
    )
    if not _ok(proc):
        return []
    return (_data(proc).get("sheets")) or []


def get_sheet_id_by_date(token: str, date_str: str) -> str | None:
    for s in list_sheets(token):
        if s.get("title") == date_str:
            return s.get("sheet_id")
    return None


def create_sheet(token: str, title: str) -> str | None:
    proc = _run(
        [
            "sheets", "+sheet-create",
            "--spreadsheet-token", token,
            "--title", title,
            "--row-count", "5000",
            "--col-count", "10",
        ],
        timeout=120,
    )
    if not _ok(proc):
        return None
    d = _data(proc)
    return d.get("sheet_id")


def count_last_data_row(token: str, sheet_id: str) -> int:
    proc = _run(
        ["sheets", "+csv-get", "--spreadsheet-token", token,
         "--sheet-id", sheet_id, "--format", "json"],
        timeout=120,
    )
    if not _ok(proc):
        return 1
    annotated = _data(proc).get("annotated_csv") or ""
    last = 1
    for line in annotated.splitlines():
        m = re.match(r"^\[row=(\d+)\][ \t]?(.*)$", line)
        if m and m.group(2).strip(" \t,"):
            last = max(last, int(m.group(1)))
    return last


def build_csv(env_name: str, items: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    for it in items:
        w.writerow([
            env_name,
            it.get("feed_id") or "",
            it.get("title") or "",
            it.get("url") or "",
            it.get("description") or "",
            it.get("_pub_local") or "",
            it.get("_push_local") or "",
        ])
    return buf.getvalue()


def append_rows(token: str, sheet_id: str, env_name: str, items: list[dict]) -> bool:
    if not items:
        return True
    start = count_last_data_row(token, sheet_id) + 1
    csv_text = build_csv(env_name, items)
    proc = _run(
        ["sheets", "+csv-put", "--spreadsheet-token", token,
         "--sheet-id", sheet_id,
         "--start-cell", f"A{start}",
         "--csv", "-"],
        stdin_text=csv_text,
        timeout=300,
    )
    if not _ok(proc):
        log.warning("写入 %s 失败: %s", sheet_id, proc.stderr[:200] if proc else "")
        return False
    return True


def ensure_day_sheet(token: str, date_str: str) -> str | None:
    sid = get_sheet_id_by_date(token, date_str)
    if sid:
        return sid
    sid = create_sheet(token, date_str)
    if not sid:
        return None
    header = _run(
        ["sheets", "+csv-put", "--spreadsheet-token", token,
         "--sheet-id", sid,
         "--start-cell", "A1",
         "--csv", ",".join(_HEADERS)],
        timeout=60,
    )
    if not _ok(header):
        log.warning("表头写入失败 %s", date_str)
    return sid


def select_expired(sheet_titles: list[tuple[str, str]], today, keep_days: int) -> list[str]:
    cutoff = today - timedelta(days=keep_days)
    expired: list[str] = []
    for sheet_id, title in sheet_titles:
        m = _DATE_RE.match(title or "")
        if not m:
            continue
        try:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
        except ValueError:
            continue
        if d < cutoff:
            expired.append(sheet_id)
    return expired


def prune_old_tabs(token: str, today, keep_days: int = 365) -> int:
    sheets = list_sheets(token)
    expired = select_expired([(s.get("sheet_id"), s.get("title")) for s in sheets], today, keep_days)
    deleted = 0
    for sid in expired:
        if _ok(_run(["sheets", "+sheet-delete", "--spreadsheet-token", token,
                     "--sheet-id", sid])):
            deleted += 1
    if deleted:
        log.info("已清理 %d 个过期日期工作表（>%d 天）", deleted, keep_days)
    return deleted


def set_tenant_readonly(token: str) -> bool:
    r1 = _run(
        ["drive", "permission.public", "patch", "--token", token,
         "--type", "sheet", "--data", '{"external_access": false}', "--yes"]
    )
    r2 = _run(
        ["drive", "permission.public", "patch", "--token", token,
         "--type", "sheet", "--data", '{"link_share_entity": "tenant_readable"}', "--yes"]
    )
    return _ok(r1) and _ok(r2)


def ensure_initialized(archive, app_env: str) -> dict:
    if archive.spreadsheet_token:
        return {"spreadsheet_token": archive.spreadsheet_token,
                "url": archive.url or base_url(archive.spreadsheet_token)}
    title = _TITLES.get(app_env, BASE_TITLE_DEFAULT)
    created = create_spreadsheet(title)
    archive.spreadsheet_token = created["spreadsheet_token"]
    archive.url = created["url"]
    log.info("已创建电子表格：%s → %s", title, created["url"])
    return dict(created)


BASE_TITLE_DEFAULT = "AI 资讯归档"


def _fmt_local(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None
    return dt.strftime("%Y-%m-%d %H:%M")


def annotate(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        c = dict(it)
        c["_pub_local"] = _fmt_local(it.get("published_at"))
        c["_push_local"] = _fmt_local(it.get("pushed_at"))
        out.append(c)
    return out


def group_by_local_day(items: list[dict]) -> dict:
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    groups: dict[str, list[dict]] = {}
    for it in items:
        day = (it.get("_push_local") or today)[:10]
        groups.setdefault(day, []).append(it)
    return dict(sorted(groups.items()))


def persist_token(app_env: str, info: dict) -> None:
    """把新建的 spreadsheet_token 回写到本地 config-{env}.yaml（避免重复建文件）。"""
    import re
    from pathlib import Path

    p = Path(f"config-{app_env}.yaml")
    if not p.exists() or not info.get("spreadsheet_token"):
        return
    src = p.read_text(encoding="utf-8")
    new_src = src.replace('spreadsheet_token: ""',
                          f'spreadsheet_token: "{info["spreadsheet_token"]}"')
    if not info.get("url"):
        new_src = re.sub(r"(archive:\n(?:.*\n)*?  url:) """,
                         r' "' + base_url(info["spreadsheet_token"]) + '"', new_src)
    elif 'url: ""' in new_src:
        new_src = new_src.replace('url: ""', f'url: "{info["url"]}"', 1)
    if new_src != src:
        p.write_text(new_src, encoding="utf-8")
        log.info("已回写 token 到 %s", p)


def sync_env(archive, app_env: str, conn) -> int:
    from feedkicker import store

    info = ensure_initialized(archive, app_env)
    unsynced = annotate(store.select_unsynced(conn))
    seen: set[str] = set()
    deduped = []
    for it in unsynced:
        key = canonicalize(it.get("url") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        deduped.append(it)
    skipped = len(unsynced) - len(deduped)
    if skipped:
        log.info("跨源去重跳过 %d 条", skipped)
    unsynced = deduped
    log.info("归档待同步 %d 条 → %s", len(unsynced), info["url"])
    if not unsynced:
        return 0
    token = info["spreadsheet_token"]
    by_day = group_by_local_day(unsynced)
    for day_str, day_items in by_day.items():
        done_key = f"arch_done_{app_env}_{day_str}"
        prev = int(store.get_meta(conn, done_key, "0") or 0)
        surplus = day_items[prev:]
        if not surplus:
            continue
        sid = ensure_day_sheet(token, day_str)
        if not sid:
            raise RuntimeError(f"创建工作表 {day_str} 失败")
        if not append_rows(token, sid, app_env, surplus):
            raise RuntimeError(f"写入工作表 {day_str} 失败")
        store.set_meta(conn, done_key, str(prev + len(surplus)))
    store.mark_synced(conn, store.select_unsynced(conn), utc_now_iso())
    prune_old_tabs(token, datetime.now().astimezone().date())
    return len(unsynced)


if __name__ == "__main__":
    import argparse

    from feedkicker import store
    from feedkicker.config import load_config

    parser = argparse.ArgumentParser(prog="tc-archive")
    parser.add_argument("--env", default=None, choices=["dev", "test", "prod"])
    parser.add_argument("--init", action="store_true", help="设置组织内只读分享")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(app_env=args.env)
    if not cfg.archive.enabled:
        log.info("archive 未启用（%s）", cfg.app_env)
        raise SystemExit(0)

    if args.init:
        ok = set_tenant_readonly(cfg.archive.spreadsheet_token)
        log.info("组织内只读分享设置：%s", "成功" if ok else "失败")

    conn = store.connect(cfg.db_path)
    try:
        n = sync_env(cfg.archive, cfg.app_env, conn)
        log.info("同步完成：%d 条", n)
        persist_token(args.env or os.environ.get("TC_APP_ENV") or "prod",
                      {"spreadsheet_token": cfg.archive.spreadsheet_token,
                       "url": cfg.archive.url})
    finally:
        conn.close()
