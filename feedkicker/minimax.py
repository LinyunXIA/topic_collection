"""MiniMax 大纲生成 facade：调用拆至 minimax_transport，解析拆至 minimax_parse（#346）。"""

from __future__ import annotations

from typing import Any

import httpx as httpx

from feedkicker.minimax_parse import parse_outline_from_response as _parse_outline_from_response
from feedkicker.minimax_schema import _TOOL_GENERATE_PPT_OUTLINE as _TOOL_GENERATE_PPT_OUTLINE
from feedkicker.minimax_schema import PROMPT_TEMPLATES as PROMPT_TEMPLATES
from feedkicker.minimax_transport import (
    _RETRY_CODES as _RETRY_CODES,
)
from feedkicker.minimax_transport import (
    _extract_code as _extract_code,
)
from feedkicker.minimax_transport import (
    _norm_code as _norm_code,
)
from feedkicker.minimax_transport import (
    _resolve_api_key as _resolve_api_key,
)
from feedkicker.minimax_transport import (
    call_minimax_chat as call_minimax_chat,
)


def gen_outline(
    topic: str,
    kind: str = "tool",
    api_key: str | None = None,
    base_url: str = "https://api.minimaxi.com",
    model: str = "MiniMax-M3",
) -> dict[str, Any]:
    if kind not in PROMPT_TEMPLATES:
        raise ValueError(f"未知 kind: {kind}，可选 {list(PROMPT_TEMPLATES)}")
    system_prompt = PROMPT_TEMPLATES[kind]
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": topic},
    ]
    data = call_minimax_chat(messages, model=model, api_key=api_key, base_url=base_url)
    return _parse_outline_from_response(data)
