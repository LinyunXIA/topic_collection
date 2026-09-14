"""第七轮审计 P1（#287）——httpx INFO 日志泄露 webhook token（失败优先验收）。

全离线：`httpx.MockTransport` 产生真实 httpx 请求日志（含完整 URL），
不打真网、无真实子进程。
"""

from __future__ import annotations

import logging

import httpx

from feedkicker import feishu, log_setup

_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/SUPER_SECRET_TOKEN_9f2c"


def test_setup_logging_silences_httpx_and_httpcore() -> None:
    log_setup.setup_logging()
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING


def test_send_does_not_leak_webhook_token_into_logs(monkeypatch, caplog) -> None:
    """httpx 默认在 INFO 记录完整请求 URL；webhook URL 即凭据，不得落日志（#287）。"""

    def fake_post(url, content=None, timeout=None, headers=None):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": 0})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            return client.post(url, content=content, timeout=timeout, headers=headers)

    monkeypatch.setattr(feishu.httpx, "post", fake_post)
    log_setup.setup_logging()

    with caplog.at_level(logging.INFO):
        assert feishu.send({"msg_type": "text"}, _WEBHOOK, 5, "ua") is True

    assert "SUPER_SECRET_TOKEN_9f2c" not in caplog.text
    assert "bot/v2/hook/" not in caplog.text
