"""MiniMax function-calling 大纲生成的 prompt 模板与 tool schema。"""

from __future__ import annotations

PROMPT_TEMPLATES: dict[str, str] = {
    "tool": (
        "你是PPT大纲生成助手（工具类）。请围绕用户给定的话题，生成一份面向实操的PPT大纲。"
        "要求：5-8页自适应，覆盖背景/痛点、工具选型/流程、分步操作、案例演示、避坑与总结；"
        "每页 heading + 3-4 条 bullets + speaker_note；通过 generate_ppt_outline 工具返回JSON。"
    ),
    "principle": (
        "你是PPT大纲生成助手（原理类）。请围绕用户给定的话题，生成一份面向原理与深度的PPT大纲。"
        "要求：5-8页自适应，覆盖背景/问题定义、核心原理/架构、关键机制对比、推导与验证、趋势与总结；"
        "每页 heading + 3-4 条 bullets + speaker_note；通过 generate_ppt_outline 工具返回JSON。"
    ),
}

_TOOL_GENERATE_PPT_OUTLINE = {
    "type": "function",
    "function": {
        "name": "generate_ppt_outline",
        "description": "生成PPT大纲，返回标题与5-8页幻灯片",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "大纲总标题"},
                "slides": {
                    "type": "array",
                    "description": "幻灯片列表，5-8页",
                    "items": {
                        "type": "object",
                        "properties": {
                            "heading": {"type": "string", "description": "页标题"},
                            "bullets": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "要点，3-4条",
                            },
                            "speaker_note": {"type": "string", "description": "讲者备注"},
                        },
                        "required": ["heading", "bullets"],
                    },
                    "minItems": 5,
                    "maxItems": 8,
                },
            },
            "required": ["title", "slides"],
        },
    },
}
