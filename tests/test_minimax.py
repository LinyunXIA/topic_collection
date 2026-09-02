from __future__ import annotations

import json as _json

import pytest

import feedkicker.minimax as mm


class FakeResp:
    def __init__(self, status_code=200, json_data=None, text_data=None):
        self.status_code = status_code
        self._json = json_data
        self.text = text_data if text_data is not None else _json.dumps(json_data or {}, ensure_ascii=False)

    def json(self):
        if self._json is not None:
            return self._json
        return _json.loads(self.text)


def test_prompt_templates_exist():
    assert "tool" in mm.PROMPT_TEMPLATES
    assert "principle" in mm.PROMPT_TEMPLATES
    assert len(mm.PROMPT_TEMPLATES["tool"]) > 10
    assert len(mm.PROMPT_TEMPLATES["principle"]) > 10
    assert mm.PROMPT_TEMPLATES["tool"][:10]


def test_call_minimax_chat_payload_and_headers(monkeypatch):
    captured: dict = {}

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        payload = json
        captured["url"] = url
        captured["json"] = payload
        captured["headers"] = headers
        outline = {
            "title": "测试大纲",
            "slides": [
                {"heading": f"第{i}页", "bullets": [f"要点{i}-{j}" for j in range(3)], "speaker_note": "备注"}
                for i in range(1, 7)
            ],
        }
        data = {
            "id": "chat-1",
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {"function": {"name": "generate_ppt_outline", "arguments": _json.dumps(outline, ensure_ascii=False)}}
                        ]
                    }
                }
            ],
        }
        return FakeResp(200, data)

    monkeypatch.setattr(mm.httpx, "post", fake_post)
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "topic"}]
    data = mm.call_minimax_chat(msgs, api_key="sk-test")
    assert captured["url"] == "https://api.minimaxi.com/v1/chat/completions"
    assert captured["json"]["model"] == "MiniMax-M3"
    assert captured["json"]["temperature"] == 0.7
    assert captured["json"]["reasoning_split"] is True
    assert any(t["function"]["name"] == "generate_ppt_outline" for t in captured["json"]["tools"])
    assert captured["json"]["tool_choice"]["function"]["name"] == "generate_ppt_outline"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert "choices" in data


def test_call_minimax_chat_custom_base_url(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None, **kw):  # noqa: B023
        captured["url"] = url
        return FakeResp(200, {"choices": [{"message": {"tool_calls": [{"function": {"name": "generate_ppt_outline", "arguments": _json.dumps({"title": "t", "slides": []})}}]}}]})

    monkeypatch.setattr(mm.httpx, "post", fake_post)
    mm.call_minimax_chat([{"role": "user", "content": "hi"}], api_key="sk", base_url="https://api.minimaxi.com/")
    assert captured["url"] == "https://api.minimaxi.com/v1/chat/completions"


def test_minimax_key_env_override(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        captured["auth"] = headers.get("Authorization")
        return FakeResp(200, {"choices": [{"message": {"tool_calls": [{"function": {"name": "generate_ppt_outline", "arguments": _json.dumps({"title": "t", "slides": []})}}]}}]})

    monkeypatch.setattr(mm.httpx, "post", fake_post)
    monkeypatch.setenv("MiniMax_Key", "sk-env-123")
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    mm.call_minimax_chat([{"role": "user", "content": "hi"}])
    assert captured["auth"] == "Bearer sk-env-123"
    monkeypatch.delenv("MiniMax_Key", raising=False)


def test_minimax_key_minimax_api_key_fallback(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        captured["auth"] = headers.get("Authorization")
        return FakeResp(200, {"choices": [{"message": {"tool_calls": [{"function": {"name": "generate_ppt_outline", "arguments": _json.dumps({"title": "t", "slides": []})}}]}}]})

    monkeypatch.setattr(mm.httpx, "post", fake_post)
    monkeypatch.delenv("MiniMax_Key", raising=False)
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-minimax-999")
    mm.call_minimax_chat([{"role": "user", "content": "hi"}])
    assert captured["auth"] == "Bearer sk-minimax-999"
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)


def test_gen_outline_tool_calls_parse(monkeypatch):
    outline = {
        "title": "AI 工具大纲",
        "slides": [
            {"heading": f"Slide {i}", "bullets": ["a", "b", "c"], "speaker_note": "note"}
            for i in range(1, 7)
        ],
    }

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        payload = json
        assert payload["model"] == "MiniMax-M3"
        assert payload["temperature"] == 0.7
        assert payload["reasoning_split"] is True
        data = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {"function": {"name": "generate_ppt_outline", "arguments": _json.dumps(outline, ensure_ascii=False)}}
                        ],
                        "content": "",
                    }
                }
            ]
        }
        return FakeResp(200, data)

    monkeypatch.setattr(mm.httpx, "post", fake_post)
    result = mm.gen_outline("话题：AI编程工具", kind="tool", api_key="sk-test")
    assert result["title"] == "AI 工具大纲"
    assert len(result["slides"]) == 6
    assert result["slides"][0]["bullets"] == ["a", "b", "c"]


def test_gen_outline_principle_kind(monkeypatch):
    outline = {"title": "原理大纲", "slides": [{"heading": f"h{i}", "bullets": ["x", "y", "z"]} for i in range(5)]}

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        payload = json
        msgs = payload["messages"]
        assert any("原理" in m["content"] for m in msgs)
        data = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {"function": {"name": "generate_ppt_outline", "arguments": _json.dumps(outline, ensure_ascii=False)}}
                        ]
                    }
                }
            ]
        }
        return FakeResp(200, data)

    monkeypatch.setattr(mm.httpx, "post", fake_post)
    result = mm.gen_outline("话题：Transformer 原理", kind="principle", api_key="sk")
    assert result["title"] == "原理大纲"
    assert len(result["slides"]) == 5


def test_gen_outline_fallback_content(monkeypatch):
    outline = {"title": "回落大纲", "slides": [{"heading": "h1", "bullets": ["a", "b"]}]}

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        data = {
            "choices": [
                {"message": {"content": _json.dumps(outline, ensure_ascii=False), "tool_calls": []}}
            ]
        }
        return FakeResp(200, data)

    monkeypatch.setattr(mm.httpx, "post", fake_post)
    result = mm.gen_outline("话题", kind="tool", api_key="sk")
    assert result["title"] == "回落大纲"


def test_retry_1004_once_then_success(monkeypatch):
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        calls.append(1)
        if len(calls) == 1:
            return FakeResp(200, {"base_resp": {"status_code": 1004, "status_msg": "auth"}})
        outline = {"title": "重试成功", "slides": [{"heading": "h", "bullets": ["a"]}]}
        return FakeResp(
            200,
            {"choices": [{"message": {"tool_calls": [{"function": {"name": "generate_ppt_outline", "arguments": _json.dumps(outline)}}]}}]},
        )

    monkeypatch.setattr(mm.httpx, "post", fake_post)
    result = mm.gen_outline("topic", kind="tool", api_key="sk")
    assert result["title"] == "重试成功"
    assert len(calls) == 2


def test_retry_1002_and_1039(monkeypatch):
    for code in (1002, 1039):
        calls = []  # noqa: B023 loop var capture intentional, handled via closure

        def fake_post(url, json=None, headers=None, timeout=None, **kw):  # noqa: B023
            calls.append(1)  # noqa: B023
            if len(calls) == 1:
                return FakeResp(200, {"base_resp": {"status_code": code, "status_msg": "retryable"}})  # noqa: B023
            outline = {"title": "ok", "slides": []}
            return FakeResp(200, {"choices": [{"message": {"tool_calls": [{"function": {"name": "generate_ppt_outline", "arguments": _json.dumps(outline)}}]}}]})

        monkeypatch.setattr(mm.httpx, "post", fake_post)
        result = mm.gen_outline("topic", kind="tool", api_key="sk")
        assert result["title"] == "ok"
        assert len(calls) == 2


def test_retry_exhausted_raises(monkeypatch):
    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        return FakeResp(200, {"base_resp": {"status_code": 1004, "status_msg": "auth"}})

    monkeypatch.setattr(mm.httpx, "post", fake_post)
    with pytest.raises(RuntimeError, match="1004"):
        mm.call_minimax_chat([{"role": "user", "content": "hi"}], api_key="sk")


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("MiniMax_Key", raising=False)
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="api_key"):
        mm.call_minimax_chat([{"role": "user", "content": "hi"}], api_key="")


def test_gen_outline_invalid_kind():
    with pytest.raises(ValueError, match="未知 kind"):
        mm.gen_outline("topic", kind="unknown", api_key="sk")


def test_arguments_string_dict_handling(monkeypatch):
    outline = {"title": "dict args", "slides": [{"heading": "h", "bullets": ["a"]}]}

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        data = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {"function": {"name": "generate_ppt_outline", "arguments": outline}}
                        ]
                    }
                }
            ]
        }
        return FakeResp(200, data)

    monkeypatch.setattr(mm.httpx, "post", fake_post)
    result = mm.gen_outline("topic", kind="tool", api_key="sk")
    assert result["title"] == "dict args"


def test_minimax_outline_mock(monkeypatch):
    outline = {
        "title": "Mock大纲",
        "slides": [
            {"heading": f"页{i}", "bullets": ["a", "b", "c"], "speaker_note": "note"}
            for i in range(1, 7)
        ],
    }

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        payload = json
        assert payload["model"] == "MiniMax-M3"
        assert payload["temperature"] == 0.7
        assert payload["reasoning_split"] is True
        data = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {"function": {"name": "generate_ppt_outline", "arguments": _json.dumps(outline, ensure_ascii=False)}}
                        ]
                    }
                }
            ]
        }
        return FakeResp(200, data)

    monkeypatch.setattr(mm.httpx, "post", fake_post)
    result = mm.gen_outline("mock topic", kind="tool", api_key="sk")
    assert result["title"] == "Mock大纲"
    assert len(result["slides"]) == 6
