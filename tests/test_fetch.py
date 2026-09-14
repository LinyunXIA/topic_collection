"""fetch_feed 真实 HTTP 边界（#166）：mock httpx.get，离线断言 UA/timeout/状态码分流。"""

from __future__ import annotations

import pytest

from feedkicker import fetch
from feedkicker.config import HttpConf

RSS = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<rss version="2.0"><channel><title>T</title><link>https://e.com</link>'
    b"<item><title>Hello</title><link>https://e.com/1</link></item>"
    b"</channel></rss>"
)


class FakeResp:
    def __init__(self, status_code: int, content: bytes = b"") -> None:
        self.status_code = status_code
        self.content = content


def test_fetch_feed_passes_ua_timeout_and_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def fake_get(url, timeout=None, headers=None, follow_redirects=False):
        calls.append(
            {
                "url": url,
                "timeout": timeout,
                "headers": headers,
                "follow_redirects": follow_redirects,
            }
        )
        return FakeResp(200, RSS)

    monkeypatch.setattr(fetch.httpx, "get", fake_get)
    http = HttpConf(timeout_seconds=7.5, user_agent="ua-x")
    entries = fetch.fetch_feed("https://feed.example/rss", http)

    assert calls == [
        {
            "url": "https://feed.example/rss",
            "timeout": 7.5,
            "headers": {"User-Agent": "ua-x"},
            "follow_redirects": True,
        }
    ]
    assert [e["title"] for e in entries] == ["Hello"]
    assert entries[0]["url"] == "https://e.com/1"


def test_fetch_feed_non_2xx_raises_before_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetch.httpx, "get", lambda url, **kw: FakeResp(503, b"busy"))
    with pytest.raises(ValueError, match="HTTP 503"):
        fetch.fetch_feed("https://feed.example/rss", HttpConf())
