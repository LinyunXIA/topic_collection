"""Provider Patch — 模型特定补丁（20% 差异部分）

声明式配置，覆盖不同 LLM provider 在 OpenAI 兼容协议上的差异。
80% 通用逻辑在 adapter.py，20% 差异在这里配置。

用法：
    from app.llm.patches import MINIMAX_PATCH
    provider = OpenAIProvider(..., patch=MINIMAX_PATCH)
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ProviderPatch:
    """模型特定补丁——声明式配置。

    请求侧：控制发给 API 的 payload 格式。
    响应侧：控制从 API 响应中提取/清理内容的方式。
    端点：控制 URL 路径（不同 API 前缀不同）。
    """

    # ── 请求侧 ──
    send_dimensions: bool = False          # embed 是否发 dimensions 参数
    dimensions_value: int = 1536           # 发 dimensions 时的值
    drop_request_fields: list[str] = field(default_factory=list)  # 要从 payload 中移除的字段
    extra_body_fields: dict = field(default_factory=dict)  # 额外请求字段

    # ── 响应侧 ──
    strip_think_tags: bool = False         # 清理 <think>...</think> 块
    strip_code_fences: bool = False        # 清理 ```json ... ``` 代码围栏
    finish_reason_map: dict = field(default_factory=dict)  # finish_reason 值映射

    # ── 端点路径 ──
    chat_path: str = "/v1/chat/completions"
    embed_path: str = "/v1/embeddings"
    models_path: str = "/v1/models"


# ── 预定义 Patch ──────────────────────────────────────────────────

OMLX_PATCH = ProviderPatch(
    send_dimensions=True,
    dimensions_value=1536,
    # Qwen3.8-27B 不产生 think 标签，json_mode 可靠
)

OPENAI_PATCH = ProviderPatch(
    # 标准 OpenAI，无特殊 patch
)

MINIMAX_PATCH = ProviderPatch(
    strip_think_tags=True,
    strip_code_fences=True,
    # MiniMax-M3 think 模型：json_mode 被忽略，content 含 think 块
)

DEEPSEEK_CHAT_PATCH = ProviderPatch(
    # deepseek-chat：标准 OpenAI 格式，无 think
    chat_path="/chat/completions",  # 无 /v1 前缀
    embed_path="/v1/embeddings",
    models_path="/v1/models",
)

DEEPSEEK_REASONER_PATCH = ProviderPatch(
    strip_think_tags=True,
    strip_code_fences=True,
    chat_path="/chat/completions",
    drop_request_fields=["temperature"],  # reasoning 模式不支持 temperature
)
