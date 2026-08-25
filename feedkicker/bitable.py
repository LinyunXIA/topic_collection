from __future__ import annotations

import json
import logging
import shutil
import subprocess
from datetime import datetime

from feedkicker.fetch import canonicalize

log = logging.getLogger(__name__)

BASE_TITLE = "AI 资讯归档"
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
]


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
    proc = _run(["base", "+title-resolve", "--keyword", title])
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


def create_base(title: str) -> dict:
    proc = _run(
        [
            "base",
            "+base-create",
            "--name",
            title,
            "--table-name",
            TABLE_NAME,
            "--fields",
            json.dumps(_FIELDS, ensure_ascii=False),
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


def create_table(app_token: str) -> str:
    proc = _run(
        [
            "base",
            "+table-create",
            "--base-token",
            app_token,
            "--name",
            TABLE_NAME,
            "--fields",
            json.dumps(_FIELDS, ensure_ascii=False),
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


def ensure_initialized(bt) -> dict:
    info: dict = {"app_token": bt.app_token, "table_id": bt.table_id, "url": bt.url}
    found = None
    if not info["app_token"]:
        found = find_base_by_title(BASE_TITLE)
        if found is None:
            found = create_base(BASE_TITLE)
        info["app_token"] = found["app_token"]
        info.setdefault("url", found.get("url") or base_url(info["app_token"]))
    if not info["table_id"]:
        info["table_id"] = get_table_id(info["app_token"]) or create_table(info["app_token"])
    return info


def _cell(item: dict) -> dict:
    def fmt_dt(iso: str | None) -> str | None:
        if not iso:
            return None
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        except ValueError:
            return None
        return dt.strftime("%Y-%m-%d %H:%M")

    return {
        "标题": item.get("title") or item.get("url") or "",
        "链接": item.get("url") or "",
        "来源": item.get("feed_id") or "",
        "摘要": item.get("description") or "",
        "发布时间": fmt_dt(item.get("published_at")),
        "推送时间": fmt_dt(item.get("pushed_at")),
    }


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


def sync_records(app_token: str, table_id: str, items: list[dict]) -> bool:
    if not items:
        return True
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
            {"create_records": [_cell(it) for it in chunk]}, ensure_ascii=False
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


if __name__ == "__main__":
    import argparse

    from feedkicker.config import load_config
    from feedkicker import store

    parser = argparse.ArgumentParser(prog="tc-bitable")
    parser.add_argument("--env", default=None, choices=["dev", "test", "prod"])
    parser.add_argument("--init", action="store_true", help="初始化/补全 Base 结构与权限")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(app_env=args.env)
    if not cfg.bitable.enabled:
        log.info("bitable 未启用（%s）", cfg.app_env)
        raise SystemExit(0)

    info = ensure_initialized(cfg.bitable)
    if args.init:
        if setup_view(info["app_token"], info["table_id"]):
            log.info("视图分组/排序已设置")
        if set_tenant_readonly(info["app_token"]):
            log.info("分享已设为组织内只读")
        print(json.dumps(info, ensure_ascii=False))

    conn = store.connect(cfg.db_path)
    items = store.select_unsynced(conn)
    log.info("待同步 %d 条 → %s/%s", len(items), info["app_token"], info["table_id"])
    if sync_records(info["app_token"], info["table_id"], items):
        from feedkicker.fetch import utc_now_iso

        store.mark_synced(conn, items, utc_now_iso())
        log.info("同步完成并打标")
    else:
        log.error("存在写入失败的批次，未打标，下次重试")
    conn.close()
