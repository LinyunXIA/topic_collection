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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://EXAMPLE.com/p?x=1#frag", "https://example.com/p?x=1"),
        ("http://[::1]:8080/p?x=1#f", "http://[::1]:8080/p?x=1"),
        ("http://[::1]/p#f", "http://[::1]/p"),
        ("http://example.com:80/a", "http://example.com/a"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("https://example.com:8443/a", "https://example.com:8443/a"),
        ("http://user:pw@EXAMPLE.com:8080/a", "http://user:pw@example.com:8080/a"),
        ("https://例え.jp/パス", "https://xn--r8jz45g.jp/パス"),
    ],
)
def test_canonicalize_boundaries(raw: str, expected: str) -> None:
    """#207：IPv6 方括号/userinfo/默认端口省略/IDNA 归一，query 保留、fragment 去除。"""
    assert fetch.canonicalize(raw) == expected
