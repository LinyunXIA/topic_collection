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


# ── extract_entities ─────────────────────────────────────────────────
# DESIGN §4.6.1 关键约束：relations.subject/object 必须引用已抽取实体的 canonical_name_zh（不是 name/surface）
EXTRACT_ENTITIES_SYSTEM = "你是一个实体关系抽取助手，严格按 JSON 输出，relations 的 subject/object 必须使用实体的 canonical_name_zh（中文规范名），不要用英文 name 或 surface。"
EXTRACT_ENTITIES_USER = """请从以下文章抽取实体与关系，输出严格 JSON。

要求：
1. 抽取所有关键实体，字段：{{"name": "原文名", "surface": "原文出现子串（必须是原文子串）", "type": "person|org|product|model|technology|concept|event|location|other", "aliases": ["别名1"], "description": "一句话描述", "canonical_name_zh": "中文规范名", "confidence": 0.0-1.0}}
2. type 枚举限上述 9 种
3. 跨语言归一：所有实体必须有 canonical_name_zh（英文原文也要给中文规范名），aliases 收别名
4. 关系抽取：{{"subject": "canonical_name_zh", "predicate": "关系名", "object": "canonical_name_zh", "confidence": 0.0-1.0, "evidence_span": "原文证据"}}；subject/object 必须是已抽取实体的 canonical_name_zh，不要用 name/surface，否则映射失败、关系静默丢失
5. grounding：surface 必须是原文子串，找不到则丢弃

输出格式（严格 JSON）：
{{
  "entities": [{{"name": "Qwen3", "surface": "Qwen3", "type": "model", "aliases": ["通义千问3"], "description": "阿里发布的大模型", "canonical_name_zh": "通义千问3", "confidence": 0.92}}],
  "relations": [{{"subject": "通义千问3", "predicate": "developed_by", "object": "阿里巴巴", "confidence": 0.85, "evidence_span": "Qwen3 由阿里巴巴..."}}]
}}

文章信息：
标题：{title}
语言：{lang}
正文：
{content}
"""

# ── generate_report ──────────────────────────────────────────────────
GENERATE_REPORT_SYSTEM = "你是一个报告撰写助手，基于统计数据撰写中文 Markdown 报告，不允许编造统计量。"
GENERATE_REPORT_USER = """请基于以下统计数据撰写报告。

要求：
1. 中性纪实风格，按 5 章结构：概览、Top主题、实体增长、源健康、潜在异常
2. 只引用 stats 中的数字，不要编造

报告类型：{report_type}
周期：{period_start} 至 {period_end}
统计：
{stats}

输出：中文 Markdown 报告内容
"""


def get_prompt(task: str, **kwargs) -> tuple[str, str]:
    """获取指定任务的 (system, user) 提示词对。"""
    # 注意：不要用 dict 字面量，Python 会立即计算所有 value 的 .format()
    # 只 format 需要的那一个
    templates = {
        "summarize": (SUMMARIZE_SYSTEM, SUMMARIZE_USER),
        "classify_topics": (CLASSIFY_TOPICS_SYSTEM, CLASSIFY_TOPICS_USER),
        "generate_wiki_entry": (WIKI_ENTRY_SYSTEM, WIKI_ENTRY_USER),
        "translate": (TRANSLATE_SYSTEM, TRANSLATE_USER),
        "extract_entities": (EXTRACT_ENTITIES_SYSTEM, EXTRACT_ENTITIES_USER),
        "generate_report": (GENERATE_REPORT_SYSTEM, GENERATE_REPORT_USER),
    }
    if task not in templates:
        raise ValueError(f"未知任务: {task}，可用: {list(templates.keys())}")
    system, user_template = templates[task]
    return system, user_template.format(**kwargs)
