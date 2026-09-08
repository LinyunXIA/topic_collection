"""Wiki「首页」自动索引页：node-list 拉大纲 docx 子节点 → 生成按月大块索引 markdown → docs +update 整篇覆盖首页。

数据源用 wiki +node-list（即时可靠，规避 node-get 对新建节点的 131005 延迟），
不依赖 sqlite；主页原模板内容每次被 markdown 整篇 overwrite 抹去。
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import re
import sys
import tempfile
from datetime import datetime

from feedkicker import bitable, wiki, wiki_lark
from feedkicker.config import load_config

log = logging.getLogger(__name__)

TITLE_RE = re.compile(r"^(?P<topic>.+)_(?P<date>\d{4}-\d{2}-\d{2})_大纲$")


def list_outline_docs(space_id: str, parent_node_token: str) -> list[dict[str, str]]:
    """列首页节点下的大纲 docx：过滤标题符合 {话题}_{日期}_大纲 ，按日期降序（同日按话题升序）。

    node-list 失败（rc≠0 / ok:false / 无 lark-cli）抛 RuntimeError，由调用方决定降级。
    """
    proc = wiki_lark.lark_node_list(space_id, parent_node_token)
    ok, _ = bitable._parse(proc)
    if not ok:
        raw = ""
        if proc is not None:
            raw = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"wiki +node-list 失败: {raw[:200]}")
    nodes = wiki_lark.parse_node_list(proc)
    docs: list[dict[str, str]] = []
    for node in nodes:
        obj_type = str(node.get("obj_type") or node.get("objType") or "")
        title = str(node.get("title") or "")
        token = str(node.get("node_token") or node.get("nodeToken") or "")
        m = TITLE_RE.match(title)
        if obj_type != "docx" or not m or not token:
            continue
        docs.append(
            {
                "date": m.group("date"),
                "topic": m.group("topic"),
                "title": title,
                "node_token": token,
                "url": wiki.wiki_url(token),
            }
        )
    docs.sort(key=lambda d: d["topic"])
    docs.sort(key=lambda d: d["date"], reverse=True)
    return docs


def _md_cell(text: str) -> str:
    """markdown 表格单元格安全：去换行、竖线转义。"""
    return text.replace("|", "／").replace("\n", " ").strip()


def build_home_md(docs: list[dict[str, str]], now: datetime | None = None) -> str:
    """纯函数：大纲条目 → 首页整篇 markdown（月块最新在最上，月内日期降序）。"""
    ref = now or datetime.now(wiki.SHANGHAI)
    today = ref.strftime("%Y-%m-%d")
    lines = [
        "# AI 沙龙双大纲归档",
        "",
        f"> 本页由 feedkicker 每周五自动重建（最近更新 {today}），共 {len(docs)} 篇。",
        "",
    ]
    months: dict[str, list[dict[str, str]]] = {}
    for d in docs:
        months.setdefault(d["date"][:7], []).append(d)
    for ym in sorted(months, reverse=True):
        year, month = ym.split("-")
        lines.append(f"## {int(year)}年{int(month)}月")
        lines.append("")
        lines.append("| 生成日期 | 文件名 | 链接 |")
        lines.append("|---|---|---|")
        for d in months[ym]:
            topic = _md_cell(d["topic"])
            lines.append(f"| {d['date']} | {topic} | [打开]({d['url']}) |")
        lines.append("")
    return "\n".join(lines)


@contextlib.contextmanager
def _home_temp_file(md_content: str):
    """lark-cli --content 只接受 cwd 内相对路径 @file，落临时 md（同 wiki._md_temp_file 约定）。"""
    fd, tmp_path = tempfile.mkstemp(prefix=".wiki-home-", suffix=".md", dir=".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(md_content)
        yield f"./{os.path.basename(tmp_path)}"
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)


def update_homepage(
    space_id: str,
    parent_node_token: str,
    dry_run: bool = False,
    now: datetime | None = None,
) -> bool:
    """重建首页索引：node-list → build markdown → docs +update overwrite。任何失败仅 WARNING 返回 False。"""
    try:
        docs = list_outline_docs(space_id, parent_node_token)
    except Exception as e:  # noqa: BLE001
        log.warning("Wiki 首页更新：拉取子节点失败: %s", e)
        return False
    md = build_home_md(docs, now=now)
    if dry_run:
        print("=== Wiki 首页预览（dry-run，不写入）===")
        print(md)
        return True
    with _home_temp_file(md) as rel:
        proc = wiki_lark.lark_doc_overwrite_md(parent_node_token, rel)
    ok, _ = bitable._parse(proc)
    if not ok:
        raw = ""
        if proc is not None:
            raw = (proc.stderr or proc.stdout or "").strip()
        log.warning("Wiki 首页 overwrite 失败: %s", raw[:300])
        return False
    log.info("Wiki 首页已重建：%d 篇大纲索引", len(docs))
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="feedkicker.wiki_home",
        description="重建 Wiki 首页：按月大块的沙龙大纲索引表格（整篇 overwrite）",
    )
    parser.add_argument("--dry-run", action="store_true", help="仅打印索引 markdown 不写入")
    parser.add_argument("--config", default=None, help="指定 config-{env}.yaml 路径")
    parser.add_argument("--db", default=None, help="sqlite 路径（覆盖推导）")
    parser.add_argument(
        "--env",
        default=None,
        choices=["dev", "test", "prod"],
        help="运行环境（覆盖 TC_APP_ENV）",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        cfg = load_config(args.config, args.db, app_env=args.env)
    except Exception as e:  # noqa: BLE001
        log.error("%s", e)
        return 2
    space_id = cfg.wiki.space_id or cfg.salon.wiki_space_id
    parent = cfg.wiki.parent_token or cfg.salon.wiki_parent_token
    if not space_id or not parent:
        log.error("wiki.space_id / wiki.parent_token 未配置，无法更新首页")
        return 2
    try:
        ok = update_homepage(space_id, parent, dry_run=args.dry_run)
    except Exception:  # noqa: BLE001
        log.exception("Wiki 首页更新未捕获异常")
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
