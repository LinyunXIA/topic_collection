from __future__ import annotations

import os

DEFAULT_FEISHU_HOST = "web91vfvm7.feishu.cn"


def feishu_host() -> str:
    """飞书租户域名单点：默认生产租户，TC_FEISHU_HOST 覆盖（调用时读取，支持测试注入与换租户）。"""
    return os.environ.get("TC_FEISHU_HOST") or DEFAULT_FEISHU_HOST
