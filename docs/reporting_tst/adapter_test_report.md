# Phase 1+ 适配器层测试报告

> 日期：2026-08-19
> 前置：Phase 1+ 外部 API + fetch --count 116/116 已通过（见 phase1plus_test_report.md）
> Python：3.14.6 · pytest：9.1.1 · SQLAlchemy：2.0.52 · asyncpg：0.31.0
> DB：PostgreSQL 17 + pgvector 0.8.6（Docker, port 5433）
> LLM：FakeLLMProvider（内存 mock）+ OpenAIProvider/OMLXProvider（mock HTTP）

---

## 测试结果总览

```
============================= 148 passed in 3.07s ==============================
```

| 类别 | 适配器层前 | 本次新增 | 合计 |
|---|---|---|---|
| Adapter 测试 | 0 | 22 | 22 |
| OpenAI Provider 测试 | 26 | 0 | 26 |
| 横切测试 | 15 | 0 | 15 |
| 集成测试 | 30 | 0 | 30 |
| 搜索测试 | 12 | 0 | 12 |
| 主题+Wiki 测试 | 15 | 0 | 15 |
| 单元测试 | 18 | 0 | 18 |
| **合计** | **116** | **32** | **148** |

**148 tests, 0 failures, 3.07s**

---

## 适配器层测试（22 tests）

### strip_think_tags（5 tests）

| 用例 | 验证点 | 状态 |
|---|---|---|
| `test_no_think_tags` | 无 think 标签 → 原文不变 | ✅ |
| `test_single_think_block` | 单个 `<think>...</think>` → 清理后只剩正文 | ✅ |
| `test_multi_line_think` | 多行 think 内容 → 整块移除 | ✅ |
| `test_no_content_after_think` | 只有 think → 返回空字符串 | ✅ |
| `test_multiple_think_blocks` | 多个相邻 think 块 → 全部移除 | ✅ |

### strip_code_fences（4 tests）

| 用例 | 验证点 | 状态 |
|---|---|---|
| `test_no_fences` | 无围栏 → 原文不变 | ✅ |
| `test_json_fence` | ` ```json ... ``` ` → 提取纯 JSON | ✅ |
| `test_plain_fence` | ` ``` ... ``` ` → 提取内容 | ✅ |
| `test_fence_without_closing` | 无闭合围栏 → 提取 ` ```json ` 后内容 | ✅ |

### build_generate_payload（6 tests）

| 用例 | 验证点 | 状态 |
|---|---|---|
| `test_basic_payload` | model/messages/temperature 正确构建 | ✅ |
| `test_default_model_fallback` | model 为空时用 default_model | ✅ |
| `test_json_mode` | json_mode=True → `response_format: {type: json_object}` | ✅ |
| `test_max_tokens` | max_tokens 有值时加入 payload | ✅ |
| `test_drop_fields` | patch.drop_request_fields=["temperature"] → 移除 temperature | ✅ |
| `test_extra_fields` | patch.extra_body_fields → 追加额外字段 | ✅ |

### build_embed_payload（3 tests）

| 用例 | 验证点 | 状态 |
|---|---|---|
| `test_no_dimensions` | 默认 patch → 不发 dimensions | ✅ |
| `test_with_dimensions` | OMLX_PATCH → 发 dimensions=1536 | ✅ |
| `test_model_override` | model 参数覆盖 default_model | ✅ |

### parse_generate_response（5 tests）

| 用例 | 验证点 | 状态 |
|---|---|---|
| `test_basic_response` | 标准响应 → text/finish_reason/usage 正确 | ✅ |
| `test_think_tag_stripped` | MINIMAX_PATCH → think 标签被清理 | ✅ |
| `test_code_fence_stripped` | MINIMAX_PATCH → think + 围栏都清理，纯 JSON | ✅ |
| `test_finish_reason_mapped` | patch.finish_reason_map={"length":"stop"} → 映射生效 | ✅ |
| `test_finish_reason_not_mapped` | 无映射 → 原值保留 | ✅ |

### parse_embed_response（2 tests）

| 用例 | 验证点 | 状态 |
|---|---|---|
| `test_basic` | embeddings 按 index 排序，dim 正确 | ✅ |
| `test_sorted_by_index` | 乱序输入 → 按 index 排序输出 | ✅ |

### URL 路径（2 tests）

| 用例 | 验证点 | 状态 |
|---|---|---|
| `test_default_paths` | 默认 patch → `/v1/chat/completions` 等 | ✅ |
| `test_deepseek_paths` | DEEPSEEK_CHAT_PATCH → `/chat/completions`（无 /v1） | ✅ |

### 预定义 Patch（5 tests）

| 用例 | 验证点 | 状态 |
|---|---|---|
| `test_omlx_patch` | send_dimensions=True, strip_think=False | ✅ |
| `test_minimax_patch` | strip_think=True, strip_fences=True | ✅ |
| `test_deepseek_chat_patch` | chat_path="/chat/completions" | ✅ |
| `test_deepseek_reasoner_patch` | strip_think + drop temperature | ✅ |
| `test_openai_patch` | 标准 OpenAI，无特殊 patch | ✅ |

---

## 三家 LLM 差异覆盖

| 差异 | oMLX | MiniMax-M3 | DeepSeek-Chat | DeepSeek-Reasoner |
|---|---|---|---|---|
| think 标签 | 不出现 ✅ | strip_think_tags ✅ | 不出现 ✅ | strip_think_tags ✅ |
| json_mode | 可靠 ✅ | strip 兜底 ✅ | 可靠 ✅ | 不支持 ✅ |
| endpoint 路径 | /v1/ ✅ | /v1/ ✅ | 无 /v1/ ✅ | 无 /v1/ ✅ |
| temperature | 支持 ✅ | 支持 ✅ | 支持 ✅ | drop ✅ |
| embed dimensions | 1536 ✅ | 不发 ✅ | 可选 ✅ | N/A ✅ |

---

## 修复的 Bug

| # | Bug | 修复 |
|---|---|---|
| 1 | think 标签内联在 OpenAIProvider 中，换 provider 要改代码 | 移入 adapter.strip_think_tags()，ProviderPatch 声明式配置 |
| 2 | `_strip_think_tags` 和 `_strip_code_fences` 与 OpenAIProvider 耦合 | 抽为 adapter.py 独立函数，任意 provider 可用 |
| 3 | embed dimensions 硬编码在 OMLXProvider 中 | 移入 adapter.build_embed_payload()，由 patch 控制 |
