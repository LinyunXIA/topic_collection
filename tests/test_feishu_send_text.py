"""feishu.send_text 写路径（#166）：mock httpx.post，离线断言 text payload 与业务码分流。"""

from __future__ import annotations

import json

import pytest

from feedkicker import feishu


class FakeResp:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def test_send_text_posts_text_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def fake_post(url, content=None, timeout=None, headers=None):
        calls.append(
            {
                "url": url,
                "json": json.loads(content.decode("utf-8")),
                "timeout": timeout,
                "headers": headers,
            }
        )
        return FakeResp({"code": 0})

    monkeypatch.setattr(feishu.httpx, "post", fake_post)
    assert feishu.send_text("SOS 内容", "https://hook.test/t", 3.5, "ua-x") is True
    assert calls == [
        {
            "url": "https://hook.test/t",
            "json": {"msg_type": "text", "content": {"text": "SOS 内容"}},
            "timeout": 3.5,
            "headers": {"Content-Type": "application/json", "User-Agent": "ua-x"},
        }
    ]


def test_send_text_business_and_http_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(feishu.httpx, "post", lambda *a, **kw: FakeResp({"StatusCode": 0}))
    assert feishu.send_text("x", "https://hook.test/t", 3, "ua") is True

    monkeypatch.setattr(feishu.httpx, "post", lambda *a, **kw: FakeResp({"StatusCode": 1}))
    assert feishu.send_text("x", "https://hook.test/t", 3, "ua") is False

    monkeypatch.setattr(feishu.httpx, "post", lambda *a, **kw: FakeResp({}, status_code=500))
    assert feishu.send_text("x", "https://hook.test/t", 3, "ua") is False


def test_send_text_empty_webhook_skips_post(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbid(*a, **kw):
        raise AssertionError("空 webhook 不得发 HTTP")

    monkeypatch.setattr(feishu.httpx, "post", forbid)
    assert feishu.send_text("x", "", 3, "ua") is False
