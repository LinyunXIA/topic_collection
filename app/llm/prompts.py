"""提示词模板 — DESIGN §4.5

一律中文输出。Summarize 输出 JSON（含 confidence）。
"""

from __future__ import annotations

# ── summarize ──────────────────────────────────────────────────────
SUMMARIZE_SYSTEM = "你是一个专业的信息摘要助手，一律使用中文输出。"
SUMMARIZE_USER = """请为以下文章生成中文摘要。

要求：
1. 生成 3-5 个要点（key_points），每个要点一句话概括核心信息
2. 生成一段 100-200 字的摘要（summary_zh）
3. 给出置信度（confidence），0.0-1.0，表示你对摘要准确性的把握程度
4. 一律使用中文输出

输出格式（严格 JSON）：
{{
  "summary_zh": "摘要文本",
  "key_points": ["要点1", "要点2", "要点3"],
  "confidence": 0.85
}}

文章标题：{title}
文章正文：
{content}
"""

# ── classify_topics ────────────────────────────────────────────────
CLASSIFY_TOPICS_SYSTEM = "你是一个主题分类助手，根据文章内容判断其与给定主题的关联度。"
CLASSIFY_TOPICS_USER = """请评估以下文章与每个主题的关联度，输出 0.0-1.0 的分数。

主题列表：
{topics_json}

文章标题：{title}
文章摘要：{summary}

输出格式（严格 JSON）：
{{
  "scores": {{"主题ID": 0.87, ...}}
}}

只输出有正关联（>0.3）的主题分数。"""


# ── generate_wiki_entry ────────────────────────────────────────────
WIKI_ENTRY_SYSTEM = "你是一个知识库词条编写助手，使用中文 Markdown 格式输出。"
WIKI_ENTRY_USER = """请基于以下文章生成一篇知识库词条。

要求：
1. 使用中文 Markdown 格式
2. 包含：定义/概述、核心要点、关键细节
3. 适合独立阅读，不依赖原文
4. 200-500 字

文章标题：{title}
文章摘要：{summary}
关键要点：{key_points}

输出：中文 Markdown 词条内容"""


# ── translate ──────────────────────────────────────────────────────
TRANSLATE_SYSTEM = "你是一个专业的翻译助手，将内容翻译为简体中文。"
TRANSLATE_USER = "请将以下内容翻译为简体中文，只输出翻译结果：\n\n{content}"


def get_prompt(task: str, **kwargs) -> tuple[str, str]:
    """获取指定任务的 (system, user) 提示词对。"""
    prompts = {
        "summarize": (SUMMARIZE_SYSTEM, SUMMARIZE_USER.format(**kwargs)),
        "classify_topics": (CLASSIFY_TOPICS_SYSTEM, CLASSIFY_TOPICS_USER.format(**kwargs)),
        "generate_wiki_entry": (WIKI_ENTRY_SYSTEM, WIKI_ENTRY_USER.format(**kwargs)),
        "translate": (TRANSLATE_SYSTEM, TRANSLATE_USER.format(**kwargs)),
    }
    if task not in prompts:
        raise ValueError(f"未知任务: {task}，可用: {list(prompts.keys())}")
    return prompts[task]
