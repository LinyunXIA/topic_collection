"""Wiki 的 lark-cli 调用层：docs +create 建 docx、wiki +node-get 反查、node-list 列子节点、docs +update 整篇覆盖。"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from typing import Any

from feedkicker import bitable

log = logging.getLogger(__name__)


def lark_doc_create(rel_path: str, parent_wiki_token: str, doc_title: str):
    """docs +create --parent-token 传 wiki 节点 token 时，直接在 wiki 树内创建 docx。

    实测 obj_type=docx；--content 只接受 cwd 内相对路径 @file。
    """
    args = [
        "docs", "+create",
        "--parent-token", parent_wiki_token,
        "--title", doc_title,
        "--doc-format", "markdown",
        "--content", f"@{rel_path}",
        "--json",
    ]
    return bitable._run(args, timeout=120)


def parse_document_id(proc: subprocess.CompletedProcess[str] | None) -> str | None:
    """docs +create 成功响应 → data.document.document_id。"""
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


def lark_node_get(
    doc_token: str, attempts: int = 2, wait_seconds: float = 3.0
) -> subprocess.CompletedProcess[str] | None:
    """node-get 接受 docx obj_token/URL 反查 wiki node_token。

    进度信息走 stderr、stdout 为纯 JSON；docs +create 刚建成的节点秒级内可能
    131005 not_found（传播延迟），故短重试一次。
    """
    proc = None
    for i in range(attempts):
        proc = bitable._run(
            ["wiki", "+node-get", "--node-token", doc_token, "--json"], timeout=60
        )
        node_token, _ = parse_node(proc)
        if node_token:
            return proc
        ok, _ = bitable._parse(proc) if proc is not None else (False, None)
        if ok:
            return proc
        log.warning("wiki +node-get 第 %d/%d 次未解析到 node_token（新建节点传播延迟？）", i + 1, attempts)
        if i < attempts - 1:
            time.sleep(wait_seconds)
    return proc


def parse_node(
    proc: subprocess.CompletedProcess[str] | None,
) -> tuple[str | None, str | None]:
    """wiki +node-get 响应 → (node_token, obj_type)。"""
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
    raw_node = data.get("node")
    node: dict[str, Any] = raw_node if isinstance(raw_node, dict) else data
    nt = node.get("node_token") or node.get("nodeToken")
    ot = node.get("obj_type") or node.get("objType")
    return (
        nt if isinstance(nt, str) and nt else None,
        ot if isinstance(ot, str) and ot else None,
    )


def lark_node_list(
    space_id: str, parent_node_token: str
) -> subprocess.CompletedProcess[str] | None:
    """wiki +node-list：列父节点下直属子节点（--page-all 自动翻页）。

    实测响应为 data.nodes[]（不是 items）；node-list 即时可靠，
    规避 node-get 对新建节点的 131005 传播延迟。
    """
    args = [
        "wiki", "+node-list",
        "--space-id", space_id,
        "--parent-node-token", parent_node_token,
        "--page-all",
        "--json",
    ]
    return bitable._run(args, timeout=120)


def parse_node_list(proc: subprocess.CompletedProcess[str] | None) -> list[dict[str, Any]]:
    """wiki +node-list 响应 → data.nodes 字典列表（失败/空返回 []）。"""
    if proc is None or proc.returncode != 0:
        return []
    raw = (proc.stdout or "").strip()
    if not raw or raw[0] not in "{[":
        return []
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return []
    data = obj.get("data") if isinstance(obj, dict) else None
    if not isinstance(data, dict):
        return []
    nodes = data.get("nodes")
    if not isinstance(nodes, list):
        return []
    return [n for n in nodes if isinstance(n, dict)]


def lark_doc_overwrite_md(
    doc_token: str, rel_content_path: str
) -> subprocess.CompletedProcess[str] | None:
    """docs +update --command overwrite：以 markdown 整篇覆盖目标文档。

    --doc 接受 wiki node token（docs 动词对 wiki 节点透明）；
    --content 只接受 cwd 内相对路径 @file；成功输出非 JSON，以 rc 判定。
    """
    args = [
        "docs", "+update",
        "--doc", doc_token,
        "--command", "overwrite",
        "--doc-format", "markdown",
        "--content", f"@{rel_content_path}",
    ]
    return bitable._run(args, timeout=120)
