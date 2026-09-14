from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from feedkicker import bitable_lark, wiki_lark
from feedkicker.feishu_host import feishu_host
from feedkicker.log_setup import PLAIN_FORMAT, setup_logging

log = logging.getLogger(__name__)


def _shanghai_tz():
    try:
        return ZoneInfo("Asia/Shanghai")
    except (ValueError, OSError):
        return timezone(timedelta(hours=8))


SHANGHAI = _shanghai_tz()


def sanitize_topic(topic: str) -> str:
    if not topic:
        return "未命名"
    s = topic.replace("/", "_").replace("\\", "_").strip().lstrip("-").strip()
    return s or "未命名"


def build_filename(topic: str, date_str: str | None = None) -> str:
    if date_str is None:
        date_str = datetime.now(SHANGHAI).strftime("%Y-%m-%d")
    return f"{sanitize_topic(topic)}_{date_str}_大纲.md"


def wiki_url(token: str) -> str:
    return f"https://{feishu_host()}/wiki/{token}"


def _dry_run_token(title: str) -> str:
    safe = sanitize_topic(title)[:20] or "stub"
    return f"wiki_dry_{safe}"


def build_doc_title(topic: str, date_str: str | None = None) -> str:
    """wiki docx 节点标题：{话题}_{日期}_大纲（不带 .md）"""
    if date_str is None:
        date_str = datetime.now(SHANGHAI).strftime("%Y-%m-%d")
    return f"{sanitize_topic(topic)}_{date_str}_大纲"


def docx_url(token: str) -> str:
    return f"https://{feishu_host()}/docx/{token}"


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


def create_wiki_doc_from_md(
    _app_token: str,
    space_id: str,
    parent_wiki_token: str,
    title: str,
    md_content: str,
    dry_run: bool = False,
    date_str: str | None = None,
) -> str:
    """在 wiki 父节点下创建 docx 并写入 markdown，返回 /wiki/<node_token> 规范链接。

    流程：``docs +create --parent-token``（取 data.document.document_id）→
    ``wiki +node-get`` 反查 node_token（新建秒级传播延迟 131005，失败时降级）→
    ``wiki +node-list`` 仅按 obj_token 命中（title-only 不采信，防命中同名旧节点，#306）→ 仍失败回退 /docx/ 并发 WARNING。
    非 dry-run 且 space_id/parent 为空或占位直接抛 RuntimeError（不建孤儿 docx）。
    app_token 保留入参兼容调用方，lark-cli 自行鉴权；``drive +upload`` 只产 file 附件（#133）。
    """
    doc_title = build_doc_title(title, date_str=date_str)
    if dry_run:
        url = wiki_url(_dry_run_token(title))
        print(url)
        return url
    if not space_id or not parent_wiki_token or "<" in space_id or "<" in parent_wiki_token:
        raise RuntimeError(
            f"拒绝创建 Wiki 文档：space_id/parent 为空或占位（{space_id!r}, {parent_wiki_token!r}），会建出孤儿 docx"
        )
    if not md_content:
        md_content = f"# {title}\n"
    with _md_temp_file(md_content, build_filename(title, date_str)) as (rel_path, _fname):
        assert not os.path.isabs(rel_path), "必须用相对路径"
        assert rel_path.startswith("./"), "必须用相对路径 ./ 前缀"

        create_proc = wiki_lark.lark_doc_create(rel_path, parent_wiki_token, doc_title)
        ok, _ = bitable_lark._parse(create_proc)
        doc_id = wiki_lark.parse_document_id(create_proc)
        if not ok or not doc_id:
            raw = ""
            if create_proc is not None:
                raw = (create_proc.stderr or create_proc.stdout or "").strip()
            raise RuntimeError(
                f"Wiki docx 创建失败（docs +create ok={ok} document_id={doc_id}）: {raw[:300]}"
            )

        node_proc = wiki_lark.lark_node_get(doc_id)
        node_token, obj_type = wiki_lark.parse_node(node_proc)
        if not node_token:
            nodes = wiki_lark.parse_node_list(wiki_lark.lark_node_list(space_id, parent_wiki_token))
            hit = next(
                (n for n in nodes if (n.get("obj_token") or n.get("objToken")) == doc_id),
                None,
            )
            cand = (hit.get("node_token") or hit.get("nodeToken")) if hit is not None else None
            if isinstance(cand, str) and cand:
                node_token = cand
        if not node_token:
            log.warning(
                "wiki +node-get/node-list 均未拿到 node_token，回退 /docx/ 链接: %s",
                ((node_proc.stdout if node_proc is not None else "") or "")[:200],
            )
            return docx_url(doc_id)
        if obj_type and obj_type != "docx":
            log.warning("新建 wiki 节点 obj_type=%s（预期 docx），title=%s", obj_type, doc_title)
        return wiki_url(node_token)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="feedkicker.wiki")
    parser.add_argument("--app-token", default="")
    parser.add_argument("--space-id", default="")
    parser.add_argument("--parent-token", default="")
    parser.add_argument("--title", default="示例话题")
    parser.add_argument("--file", default=None, help="MD 文件路径，默认用 title 生成示例")
    parser.add_argument("--dry-run", action="store_true", help="仅打印 wiki_url 不真传")
    parser.add_argument("--env", default=None, choices=["dev", "test", "prod"])
    args = parser.parse_args(argv)

    setup_logging(logging.INFO, PLAIN_FORMAT)

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
        return 0

    if args.file:
        try:
            with open(args.file, encoding="utf-8") as f:
                md = f.read()
        except (OSError, UnicodeDecodeError) as e:
            log.error("读取 --file 失败: %s", e)
            return 2
    else:
        md = f"# {args.title}\n\n示例内容\n"

    app_token = args.app_token
    space_id = args.space_id
    parent = args.parent_token
    if not app_token or not space_id or not parent:
        try:
            from feedkicker.config import load_config

            cfg = load_config(app_env=args.env)
            app_token = app_token or cfg.wiki.app_token or cfg.salon.app_token
            space_id = space_id or cfg.wiki.space_id
            parent = parent or cfg.wiki.parent_token
        except Exception:  # noqa: BLE001
            pass

    try:
        url = create_wiki_doc_from_md(app_token, space_id, parent, args.title, md, dry_run=False)
    except (RuntimeError, OSError) as e:
        log.error("%s", e)
        return 2
    print(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
