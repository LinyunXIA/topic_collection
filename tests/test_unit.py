"""单元测试 — dedup / cleaner / structured / fts / config"""

from __future__ import annotations

import json

import pytest


# ── dedup ──────────────────────────────────────────────────────────

class TestDedup:
    def test_url_hash_deterministic(self):
        from app.ingest.dedup import url_hash
        h1 = url_hash("https://example.com/article/1")
        h2 = url_hash("https://example.com/article/1")
        assert h1 == h2

    def test_url_hash_different_urls(self):
        from app.ingest.dedup import url_hash
        assert url_hash("https://a.com/1") != url_hash("https://b.com/1")

    def test_url_hash_canonicalizes(self):
        from app.ingest.dedup import url_hash, canonicalize_url
        # 去 fragment、lowercase、去 trailing slash
        assert canonicalize_url("https://Example.COM/path/#frag") == "https://example.com/path"
        assert canonicalize_url("http://A.com/b/") == "http://a.com/b"

    def test_content_hash_deterministic(self):
        from app.ingest.dedup import content_hash
        h = content_hash("Hello World  test")
        assert len(h) == 64  # sha256 hex
        assert h == content_hash("Hello World  test")

    def test_content_hash_normalizes_whitespace(self):
        from app.ingest.dedup import content_hash
        assert content_hash("a  b  c") == content_hash("a b c")

    def test_content_hash_different_content(self):
        from app.ingest.dedup import content_hash
        assert content_hash("foo") != content_hash("bar")


# ── cleaner ────────────────────────────────────────────────────────

class TestCleaner:
    @pytest.mark.asyncio
    async def test_extract_content(self):
        from app.services.cleaner import extract_content
        html = "<html><body><h1>Title</h1><p>Hello world content here.</p></body></html>"
        md, text = await extract_content(html)
        assert "Hello" in text

    @pytest.mark.asyncio
    async def test_clean_article_removes_script(self):
        from app.services.cleaner import clean_article
        html = "<html><body><p>Good content</p><script>evil()</script></body></html>"
        result = await clean_article(html, "Test")
        assert result["is_parseable"]
        assert "evil" not in result["content_text"]

    @pytest.mark.asyncio
    async def test_clean_article_unparseable(self):
        from app.services.cleaner import clean_article
        result = await clean_article("", "")
        assert not result["is_parseable"]

    @pytest.mark.asyncio
    async def test_detect_language_en(self):
        from app.services.cleaner import detect_language
        lang = await detect_language("This is a longer English text for testing language detection.")
        assert lang == "en"

    @pytest.mark.asyncio
    async def test_detect_language_zh(self):
        from app.services.cleaner import detect_language
        lang = await detect_language("这是一段较长的中文测试文本，用于验证语言检测功能是否正常工作。")
        assert lang == "zh"

    @pytest.mark.asyncio
    async def test_detect_language_short_returns_unknown(self):
        from app.services.cleaner import detect_language
        lang = await detect_language("x")
        assert lang == "unknown"


# ── structured ─────────────────────────────────────────────────────

class TestStructured:
    def test_parse_json_valid(self):
        from app.llm.structured import parse_json
        assert parse_json('{"a": 1}') == {"a": 1}
        assert parse_json("") is None

    def test_extract_json_from_markdown(self):
        from app.llm.structured import extract_json_from_text
        text = 'Here is the result:\n```json\n{"key": "value"}\n```\nDone.'
        result = extract_json_from_text(text)
        assert '"key"' in result

    def test_repair_json_trailing_comma(self):
        from app.llm.structured import repair_json
        result = repair_json('{"a": 1, "b": 2,}')
        assert result == {"a": 1, "b": 2}

    def test_repair_json_single_quotes(self):
        from app.llm.structured import repair_json
        result = repair_json("{'key': 'value'}")
        assert result == {"key": "value"}

    def test_parse_with_repair_valid(self):
        from app.llm.structured import parse_with_repair
        result = parse_with_repair('{"summary_zh": "ok", "key_points": [], "confidence": 0.8}',
                                   expected_keys=["summary_zh"])
        assert result is not None
        assert result["summary_zh"] == "ok"

    def test_parse_with_repair_garbage(self):
        from app.llm.structured import parse_with_repair
        result = parse_with_repair("not json at all {{{")
        assert result is None

    def test_parse_with_repair_list_not_dict(self):
        from app.llm.structured import parse_with_repair
        result = parse_with_repair('[1, 2, 3]')
        assert result is None  # list, not dict


# ── fts ────────────────────────────────────────────────────────────

class TestFTS:
    def test_jieba_join(self):
        from app.db.fts import jieba_join
        result = jieba_join("人工智能技术")
        assert isinstance(result, str)
        assert len(result) > 0
        # tokens should be space-separated
        tokens = result.split()
        assert len(tokens) >= 1

    def test_jieba_join_mixed(self):
        from app.db.fts import jieba_join
        result = jieba_join("AI and 人工智能")
        tokens = result.split()
        assert len(tokens) >= 2


# ── prompts ────────────────────────────────────────────────────────

class TestPrompts:
    def test_get_prompt_summarize(self):
        from app.llm.prompts import get_prompt
        system, user = get_prompt("summarize", title="T", content="C")
        assert "摘要" in system
        assert "T" in user

    def test_get_prompt_no_keyerror(self):
        """关键测试：get_prompt 只 format 需要的模板，不触发其他模板的 KeyError。"""
        from app.llm.prompts import get_prompt
        # summarize 不需要 topics_json，不应报 KeyError
        system, user = get_prompt("summarize", title="T", content="C")
        assert system is not None

    def test_get_prompt_unknown_task(self):
        from app.llm.prompts import get_prompt
        with pytest.raises(ValueError, match="未知任务"):
            get_prompt("nonexistent")


# ── config ─────────────────────────────────────────────────────────

class TestConfig:
    def test_load_settings(self):
        from app.config import load_settings
        s = load_settings()
        assert s.db.vector_dim == 1536
        assert "topic_collection" in s.db.dsn

    def test_task_priority(self):
        from app.pipeline import TASK_PRIORITY
        assert TASK_PRIORITY["embed_core"] == 1
        assert TASK_PRIORITY["summarize"] == 2
        assert TASK_PRIORITY["embed_summary"] == 6
