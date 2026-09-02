from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from feedkicker import bitable

try:
    SHANGHAI = ZoneInfo("Asia/Shanghai")
except Exception:
    SHANGHAI = timezone(timedelta(hours=8))

log = logging.getLogger(__name__)

WIKI_HOST = "https://web91vfvm7.feishu.cn/wiki"
API_BASE = "https://open.feishu.cn/open-apis"


def sanitize_topic(topic: str) -> str:
    if not topic:
        return "未命名"
    s = topic.replace("/", "_").replace("\\", "_")
    s = s.strip()
    return s or "未命名"


def build_filename(topic: str, date_str: str | None = None) -> str:
    if date_str is None:
        date_str = datetime.now(SHANGHAI).strftime("%Y-%m-%d")
    return f"{sanitize_topic(topic)}_{date_str}_大纲.md"


def wiki_url(token: str) -> str:
    return f"{WIKI_HOST}/{token}"


def _dry_run_token(title: str) -> str:
    safe = sanitize_topic(title)[:20] or "stub"
    return f"wiki_dry_{safe}"


def _parse_upload_token(proc) -> str | None:
    if proc is None:
        return None
    raw = (proc.stdout or "").strip()
    if raw and raw[0] in "{[":
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            obj = {}
        if isinstance(obj, dict):
            data = obj.get("data") or obj
            for k in ("wiki_token", "wikiToken", "token", "file_token", "fileToken", "obj_token"):
                v = data.get(k)
                if isinstance(v, str) and v:
                    return v
            inner = data.get("file") or data.get("wiki") or {}
            if isinstance(inner, dict):
                for k in ("wiki_token", "token", "file_token"):
                    v = inner.get(k)
                    if isinstance(v, str) and v:
                        return v
    tok = raw.split()[-1] if raw else ""
    if tok and tok.startswith("wik"):
        return tok
    return None


@contextlib.contextmanager
def _md_temp_file(md_content: str, filename: str):
    fd, tmp_path = tempfile.mkstemp(prefix=".wiki-", suffix=".md", dir=".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(md_content)
        rel = f"./{os.path.basename(tmp_path)}"
        yield rel, filename
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)


def _lark_upload(rel_path: str, parent_wiki_token: str):
    candidates = [
        ["drive", "+upload", "--file", rel_path, "--wiki-token", parent_wiki_token],
        ["drive", "upload", "--file", rel_path, "--wiki-token", parent_wiki_token],
        ["drive", "+upload", "--file", rel_path, "--wiki_token", parent_wiki_token],
    ]
    if not parent_wiki_token:
        candidates = [
            ["drive", "+upload", "--file", rel_path],
            ["drive", "upload", "--file", rel_path],
        ] + candidates
    last = None
    for args in candidates[:1]:
        proc = bitable._run(args, timeout=120)
        last = proc
        if proc is not None and bitable._parse(proc)[0]:
            return proc
        if proc is not None and proc.returncode == 0:
            tok = _parse_upload_token(proc)
            if tok:
                return proc
    return last


def _httpx_move(app_token: str, space_id: str, parent_wiki_token: str, obj_token: str) -> str | None:
    if not space_id or not obj_token:
        return None
    url = f"{API_BASE}/wiki/v2/spaces/{space_id}/nodes/move_docs_to_wiki"
    payload = {
        "parent_wiki_token": parent_wiki_token,
        "obj_type": "docx",
        "obj_token": obj_token,
    }
    try:
        resp = httpx.post(url, json=payload, timeout=30, headers={"Authorization": f"Bearer {app_token}"} if app_token else None)
        try:
            data = resp.json()
        except Exception:
            try:
                data = json.loads(resp.text or "{}")
            except Exception:
                data = {}
        if resp.status_code in (200, 201):
            if isinstance(data, dict):
                if data.get("code") not in (None, 0) and data.get("code") != "0":
                    log.warning("wiki move 业务失败: %s", data)
                    return None
                d = data.get("data") or {}
                tok = d.get("wiki_token") or d.get("wikiToken") or d.get("token") or obj_token
                return tok
            return obj_token
        log.warning("wiki move HTTP %d: %s", resp.status_code, str(data)[:300])
    except Exception as e:
        log.warning("wiki move 异常: %s", e)
    return None


def create_wiki_doc_from_md(
    app_token: str,
    space_id: str,
    parent_wiki_token: str,
    title: str,
    md_content: str,
    dry_run: bool = False,
    date_str: str | None = None,
) -> str:
    filename = build_filename(title, date_str=date_str)
    if dry_run:
        stub = _dry_run_token(title)
        url = wiki_url(stub)
        print(url)
        return url
    if not md_content:
        md_content = f"# {title}\n"
    with _md_temp_file(md_content, filename) as (rel_path, _fname):
        assert not os.path.isabs(rel_path), "必须用相对路径"
        assert rel_path.startswith("./"), "必须用相对路径 ./ 前缀"
        proc = _lark_upload(rel_path, parent_wiki_token)
        if proc is not None:
            ok, _ = bitable._parse(proc)
            tok = _parse_upload_token(proc)
            if tok:
                return wiki_url(tok)
            if ok:
                raw = (proc.stdout or "").strip()
                if raw and "wiki" in raw.lower():
                    for part in raw.split():
                        if part.startswith("wik"):
                            return wiki_url(part.strip())
                if ok and not tok:
                    cand = raw.split("/")[-1].strip() if "/" in raw else ""
                    if cand and len(cand) > 6:
                        return wiki_url(cand)
        obj_token = _parse_upload_token(proc) if proc else None
        if not obj_token:
            obj_token = _dry_run_token(title)
        moved = _httpx_move(app_token, space_id, parent_wiki_token, obj_token)
        if moved:
            return wiki_url(moved)
        if proc is not None:
            tok = _parse_upload_token(proc)
            if tok:
                return wiki_url(tok)
        raise RuntimeError(f"Wiki 写入失败: lark-cli 与 move_docs_to_wiki 均未返回 token (title={title})")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(prog="feedkicker.wiki")
    parser.add_argument("--app-token", default="")
    parser.add_argument("--space-id", default="")
    parser.add_argument("--parent-token", default="")
    parser.add_argument("--title", default="示例话题")
    parser.add_argument("--file", default=None, help="MD 文件路径，默认用 title 生成示例")
    parser.add_argument("--dry-run", action="store_true", help="仅打印 wiki_url 不真传")
    parser.add_argument("--env", default=None, choices=["dev", "test", "prod"])
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.dry_run:
        url = create_wiki_doc_from_md(
            args.app_token or "stub_app",
            args.space_id or "stub_space",
            args.parent_token or "stub_parent",
            args.title,
            "# 示例大纲\n- a\n- b\n",
            dry_run=True,
        )
        print(json.dumps({"wiki_url": url, "filename": build_filename(args.title)}, ensure_ascii=False, indent=2))
    else:
        md = ""
        if args.file:
            with open(args.file, encoding="utf-8") as f:
                md = f.read()
        else:
            if not args.app_token or not args.space_id:
                try:
                    from feedkicker.config import load_config

                    cfg = load_config(app_env=args.env)
                    app_token = args.app_token or cfg.wiki.app_token or cfg.salon.app_token
                    space_id = args.space_id or cfg.wiki.space_id
                    parent = args.parent_token or cfg.wiki.parent_token
                except Exception:
                    app_token = args.app_token
                    space_id = args.space_id
                    parent = args.parent_token
            else:
                app_token = args.app_token
                space_id = args.space_id
                parent = args.parent_token
            md = f"# {args.title}\n\n示例内容\n"
            url = create_wiki_doc_from_md(app_token, space_id, parent, args.title, md, dry_run=False)
            print(url)
