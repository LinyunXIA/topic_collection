"""salon 双大纲 markdown 组装与 dry-run 桩大纲。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any


def topic_title(rec: dict[str, Any]) -> str:
    fields = rec.get("fields") or {}
    for k in ("话题名称", "标题", "title", "Topic", "name"):
        v = fields.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, list) and v and isinstance(v[0], str) and v[0].strip():
            return v[0].strip()
    rid = rec.get("record_id") or rec.get("id") or ""
    return rid or "未命名话题"


def outline_to_md(outline: dict[str, Any], label: str) -> str:
    title = outline.get("title") or label
    slides = outline.get("slides") or []
    lines = [f"## {label}", "", f"**{title}**", ""]
    for idx, s in enumerate(slides, 1):
        heading = s.get("heading") or f"第{idx}页"
        bullets = s.get("bullets") or []
        note = s.get("speaker_note") or s.get("speakerNote") or ""
        lines.append(f"### {idx}. {heading}")
        for b in bullets:
            lines.append(f"- {b}")
        if note:
            lines.append(f"> 备注：{note}")
        lines.append("")
    md_json = json.dumps(outline, ensure_ascii=False, indent=2)
    lines.append(f"```json\n{md_json}\n```")
    lines.append("")
    return "\n".join(lines)


def build_combined_md(
    title: str, tool_outline: dict[str, Any], principle_outline: dict[str, Any]
) -> str:
    date_str = datetime.now(UTC).strftime("%Y-%m-%d")
    header = f"# {title} · 大纲归档 {date_str}\n"
    tool_md = outline_to_md(tool_outline, "工具类大纲")
    princ_md = outline_to_md(principle_outline, "原理类大纲")
    return f"{header}\n{tool_md}\n---\n\n{princ_md}\n"


def stub_outlines(title: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """dry-run 本地桩大纲：不触 MiniMax，避免预览产生计费调用（OBS2）。"""
    tool = {
        "title": f"{title} · 工具类大纲",
        "slides": [
            {
                "heading": f"工具页{i}",
                "bullets": ["要点A", "要点B", "要点C"],
                "speaker_note": "备注",
            }
            for i in range(1, 6)
        ],
    }
    principle = {
        "title": f"{title} · 原理类大纲",
        "slides": [
            {
                "heading": f"原理页{i}",
                "bullets": ["要点A", "要点B", "要点C"],
                "speaker_note": "备注",
            }
            for i in range(1, 6)
        ],
    }
    return tool, principle
