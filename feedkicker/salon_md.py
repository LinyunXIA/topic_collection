"""salon 双大纲 markdown 组装与 dry-run 桩大纲。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from feedkicker import bitable_lark


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


def record_status(rec: dict[str, Any]) -> str:
    """「讨论状态」归一：list 取首个字符串，str 直取，其余空串（salon_flow 过滤用）。"""
    fields = rec.get("fields")
    v = fields.get("讨论状态") if isinstance(fields, dict) else None
    if isinstance(v, list):
        return v[0] if v and isinstance(v[0], str) else ""
    return v if isinstance(v, str) else ""


def _slides_of(outline: Any) -> list[dict[str, Any]]:
    """slides 归一：非 dict / slides 缺失或非列表 / 无有效页 / 页缺 bullets 均 raise（salon_flow 逐题跳过）。

    不能把空壳大纲归一为 []：`{}`/缺 slides 会让调用方照建空 Wiki 并 mark_topic_archived，
    该题被永久归档为「已选题」、不再生成，卡片却宣称成功（#263）。每页 bullets 须至少含 1 个
    str/int/float，否则近空页同样永久归档（#347）。非 dict 页项跳过。
    """
    if not isinstance(outline, dict):
        raise ValueError(f"大纲非对象（{type(outline).__name__}），无法渲染")
    slides = outline.get("slides")
    if not isinstance(slides, list):
        raise ValueError(f"大纲缺 slides 或非列表（{type(slides).__name__}），无法渲染")
    valid = [s for s in slides if isinstance(s, dict)]
    if not valid:
        raise ValueError("大纲为空或缺 slides（需 ≥1 页），无法渲染")
    for s in valid:
        bullets = s.get("bullets")
        if not isinstance(bullets, list) or not any(
            isinstance(b, (str, int, float)) for b in bullets
        ):
            raise ValueError(f"大纲页缺有效 bullets（需 ≥1 个 str/int/float）: {str(s)[:200]}")
    return valid


def outline_to_md(outline: dict[str, Any], label: str) -> str:
    if not isinstance(outline, dict):
        outline = {}
    title = outline.get("title") or label
    slides = _slides_of(outline)
    lines = [f"## {label}", "", f"**{title}**", ""]
    for idx, s in enumerate(slides, 1):
        heading = s.get("heading") or f"第{idx}页"
        if not isinstance(heading, str):
            heading = f"第{idx}页"
        bullets = [str(b) for b in (s.get("bullets") or []) if isinstance(b, (str, int, float))]
        note = s.get("speaker_note") or s.get("speakerNote") or ""
        lines.append(f"### {idx}. {heading}")
        for b in bullets:
            lines.append(f"- {b}")
        if isinstance(note, str) and note:
            lines.append(f"> 备注：{note}")
        lines.append("")
    md_json = json.dumps(outline, ensure_ascii=False, indent=2)
    lines.append(f"```json\n{md_json}\n```")
    lines.append("")
    return "\n".join(lines)


def build_combined_md(
    title: str, tool_outline: dict[str, Any], principle_outline: dict[str, Any]
) -> str:
    date_str = datetime.now(bitable_lark.SHANGHAI).strftime("%Y-%m-%d")
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
