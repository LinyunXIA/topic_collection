from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from feedkicker import bitable

try:
    SHANGHAI = ZoneInfo("Asia/Shanghai")
except Exception:
    SHANGHAI = timezone(timedelta(hours=8))

log = logging.getLogger(__name__)

WIKI_HOST = "https://web91vfvm7.feishu.cn/wiki"


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


def build_doc_title(topic: str, date_str: str | None = None) -> str:
    """wiki docx 节点标题：{话题}_{日期}_大纲（不带 .md）"""
    if date_str is None:
        date_str = datetime.now(SHANGHAI).strftime("%Y-%m-%d")
    return f"{sanitize_topic(topic)}_{date_str}_大纲"


def docx_url(token: str) -> str:
    host = WIKI_HOST.rsplit("/wiki", 1)[0]
    return f"{host}/docx/{token}"


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


def _lark_doc_create(rel_path: str, parent_wiki_token: str, doc_title: str):
    # docs +create --parent-token 传 wiki 节点 token 时，直接在 wiki 树内创建 docx
    # （实测 obj_type=docx）；--content 只接受 cwd 内相对路径 @file
    args = [
        "docs", "+create",
        "--parent-token", parent_wiki_token,
        "--title", doc_title,
        "--doc-format", "markdown",
        "--content", f"@{rel_path}",
        "--json",
    ]
    return bitable._run(args, timeout=120)


def _parse_document_id(proc) -> str | None:
    """docs +create 成功响应 → data.document.document_id"""
    if proc is None or proc.returncode != 0:
        return None
    raw = (proc.stdout or "").strip()
    if not raw or raw[0] not in "{[":
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    data = obj.get("data")
    if not isinstance(data, dict):
        return None
    doc = data.get("document")
    if isinstance(doc, dict):
        for k in ("document_id", "documentId", "doc_token"):
            v = doc.get(k)
            if isinstance(v, str) and v:
                return v
    for k in ("document_id", "documentId", "doc_token", "obj_token"):
        v = data.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def _lark_node_get(doc_token: str, attempts: int = 2, wait_seconds: float = 3.0):
    # node-get 接受 docx obj_token / URL，反查 wiki node_token（进度信息走 stderr，stdout 为纯 JSON）；
    # docs +create 刚建成的节点秒级内可能 131005 not_found（传播延迟），故短重试一次
    proc = None
    for i in range(attempts):
        proc = bitable._run(["wiki", "+node-get", "--node-token", doc_token, "--json"], timeout=60)
        node_token, _ = _parse_node(proc)
        if node_token:
            return proc
        ok, _ = bitable._parse(proc) if proc is not None else (False, None)
        if ok:
            return proc  # 业务成功但无 node_token，重试无意义
        log.warning("wiki +node-get 第 %d/%d 次未解析到 node_token（新建节点传播延迟？）", i + 1, attempts)
        if i < attempts - 1:
            time.sleep(wait_seconds)
    return proc


def _parse_node(proc) -> tuple[str | None, str | None]:
    """wiki +node-get 响应 → (node_token, obj_type)"""
    if proc is None or proc.returncode != 0:
        return None, None
    raw = (proc.stdout or "").strip()
    if not raw or raw[0] not in "{[":
        return None, None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None, None
    data = obj.get("data") if isinstance(obj, dict) else None
    if not isinstance(data, dict):
        return None, None
    node = data.get("node") if isinstance(data.get("node"), dict) else data
    nt = node.get("node_token") or node.get("nodeToken")
    ot = node.get("obj_type") or node.get("objType")
    return (
        nt if isinstance(nt, str) and nt else None,
        ot if isinstance(ot, str) and ot else None,
    )


def create_wiki_doc_from_md(
    app_token: str,
    space_id: str,
    parent_wiki_token: str,
    title: str,
    md_content: str,
    dry_run: bool = False,
    date_str: str | None = None,
) -> str:
    """在 wiki 父节点下创建 docx 并写入 markdown，返回 /wiki/<node_token> 规范链接。

    流程（lark-cli，已实测）：
    1. ``docs +create --parent-token <wiki节点> --doc-format markdown --content @file``
       直接在 wiki 树内建 docx（obj_type=docx），取 data.document.document_id；
    2. ``wiki +node-get --node-token <document_id>`` 反查 node_token 拼规范链接。
    注意：``drive +upload --wiki-token`` 只会产出 obj_type=file 的附件节点（#133），不可用。
    app_token/space_id 保留入参兼容调用方，lark-cli 自行鉴权与空间解析。
    """
    doc_title = build_doc_title(title, date_str=date_str)
    if dry_run:
        url = wiki_url(_dry_run_token(title))
        print(url)
        return url
    if not md_content:
        md_content = f"# {title}\n"
    with _md_temp_file(md_content, build_filename(title, date_str)) as (rel_path, _fname):
        assert not os.path.isabs(rel_path), "必须用相对路径"
        assert rel_path.startswith("./"), "必须用相对路径 ./ 前缀"

        create_proc = _lark_doc_create(rel_path, parent_wiki_token, doc_title)
        ok, _ = bitable._parse(create_proc)
        doc_id = _parse_document_id(create_proc)
        if not ok or not doc_id:
            raw = ""
            if create_proc is not None:
                raw = (create_proc.stderr or create_proc.stdout or "").strip()
            raise RuntimeError(
                f"Wiki docx 创建失败（docs +create ok={ok} document_id={doc_id}）: {raw[:300]}"
            )

        node_proc = _lark_node_get(doc_id)
        node_token, obj_type = _parse_node(node_proc)
        if not node_token:
            # docx 已建成且挂在 wiki 父节点下，仅 node_token 反查失败：回退 /docx/ 链接保证可用
            log.warning(
                "wiki +node-get 未返回 node_token，回退 /docx/ 链接: %s",
                ((node_proc.stdout if node_proc is not None else "") or "")[:200],
            )
            return docx_url(doc_id)
        if obj_type and obj_type != "docx":
            log.warning("新建 wiki 节点 obj_type=%s（预期 docx），title=%s", obj_type, doc_title)
        return wiki_url(node_token)


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
