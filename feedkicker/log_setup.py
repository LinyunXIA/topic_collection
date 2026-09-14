"""共享日志初始化：统一 basicConfig 并静音 httpx/httpcore 的 INFO 请求日志。

httpx 在 INFO 记录完整请求 URL，而飞书自定义机器人 webhook URL 本身即凭据
（`…/open-apis/bot/v2/hook/<token>`）；入口各自 `basicConfig(level=INFO)` 会让
httpx 继承 INFO 并把 token 明文写进 logs/*.log（#287 事故，prod 实测 38 处）。
"""

from __future__ import annotations

import logging

NAME_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
PLAIN_FORMAT = "%(asctime)s %(levelname)s %(message)s"

_QUIET_LOGGERS = ("httpx", "httpcore")


def setup_logging(level: int = logging.INFO, fmt: str = NAME_FORMAT) -> None:
    """初始化根日志并按 WARNING 静音 httpx/httpcore，防 webhook 凭据落盘（#287）。"""
    logging.basicConfig(level=level, format=fmt)
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
