# Phase 1+ 测试报告

> 日期：2026-08-19
> 前置：切片一~三 + 横切 86/86 已通过（见 slice1/2/3_test_report.md）
> Python：3.14.6 · pytest：9.1.1 · SQLAlchemy：2.0.52 · asyncpg：0.31.0
> DB：PostgreSQL 17 + pgvector 0.8.6（Docker, port 5433）
> LLM：FakeLLMProvider（内存 mock）+ OpenAIProvider（mock HTTP）

---

## 测试结果总览

```
============================= 116 passed in 3.17s ==============================
```

| 类别 | 切片一~三 | Phase 1+ 新增 | 合计 |
|---|---|---|---|
| 集成测试（PRD 验收） | 30 | 0 | 30 |
| 横切测试 | 11 | 4 | 15 |
| OpenAI Provider 测试 | 0 | 26 | 26 |
| 搜索测试 | 12 | 0 | 12 |
| 主题+Wiki 测试 | 15 | 0 | 15 |
| 单元测试 | 18 | 0 | 18 |
| **合计** | **86** | **30** | **116** |

**116 tests, 0 failures, 3.17s**

---

## Phase 1+ 新增功能覆盖

### P1+.1：外部 LLM API 切换（26 tests）

#### OpenAIProvider 端点调用（6 tests）

| 用例 | 验证点 | 状态 |
|---|---|---|
| `test_generate_calls_chat_completions` | `POST /v1/chat/completions`、`Authorization: Bearer` header、json_mode 正确 | ✅ |
| `test_generate_no_api_key_no_auth_header` | `api_key=None` 时不带 Authorization header | ✅ |
| `test_embed_calls_embeddings_endpoint` | `POST /v1/embeddings`、不传 `dimensions` 参数 | ✅ |
| `test_rerank_raises_not_implemented` | OpenAI 不支持 rerank → `NotImplementedError` | ✅ |
| `test_healthcheck_ok` | `GET /v1/models` 200 → `healthy=True` | ✅ |
| `test_healthcheck_error` | 连接失败 → `healthy=False` | ✅ |

#### `_classify_http_error` 分类（6 tests）

| 用例 | 验证点 | 状态 |
|---|---|---|
| `test_401_is_permanent` | 401 → `PermanentError` | ✅ |
| `test_403_is_permanent` | 403 → `PermanentError` | ✅ |
| `test_400_is_permanent` | 400 → `PermanentError` | ✅ |
| `test_500_is_transient` | 500 → `TransientError` | ✅ |
| `test_429_is_transient` | 429 → `TransientError` | ✅ |
| `test_200_raises_generic_exception` | 非 4xx/5xx → 普通 Exception | ✅ |

#### LLMClient HTTP 分类集成（2 tests）

| 用例 | 验证点 | 状态 |
|---|---|---|
| `test_401_raises_permanent_error` | 401 → `PermanentError`，不重试（只调用 1 次） | ✅ |
| `test_500_retries_before_failing` | 500 → 退避重试 `max_retries+1` 次后抛 `HTTPStatusError` | ✅ |

#### Factory 构建验证（7 tests）

| 用例 | 验证点 | 状态 |
|---|---|---|
| `test_build_generate_omlx` | generate → omlx provider, endpoint 含 8000 | ✅ |
| `test_build_embed_omlx` | embed → omlx provider | ✅ |
| `test_build_rerank_omlx` | rerank → omlx provider | ✅ |
| `test_build_generate_openai_requires_api_key` | openai 无 env var → `RuntimeError` fail fast | ✅ |
| `test_build_embed_rejects_non_omlx` | embed 不是 omlx → `ValueError` | ✅ |
| `test_build_rerank_rejects_non_omlx` | rerank 不是 omlx → `ValueError` | ✅ |
| `test_build_unknown_capability` | 未知 capability → `ValueError` | ✅ |

#### `_resolve_api_key`（5 tests）

| 用例 | 验证点 | 状态 |
|---|---|---|
| `test_omlx_returns_none` | omlx 后端跳过 resolve，返回 None | ✅ |
| `test_openai_no_env_name_raises` | openai 无 env_name → `RuntimeError` | ✅ |
| `test_openai_missing_env_var_raises` | env var 未设置 → `RuntimeError` | ✅ |
| `test_openai_empty_env_var_raises` | env var 为空 → `RuntimeError` | ✅ |
| `test_openai_valid_env_var_returns_key` | env var 有值 → 返回 key | ✅ |

---

### P1+.2：`tc feeds fetch --count N`（4 tests）

| 用例 | 验证点 | 状态 |
|---|---|---|
| `test_items_truncated_to_count` | 20 条 items + `--count 5` → 只取前 5 条 | ✅ |
| `test_count_none_no_truncation` | `count=None` 时不截断，保持 20 条 | ✅ |
| `test_count_larger_than_items_no_truncation` | count > items 数量 → 不截断 | ✅ |
| `test_fetch_count_limited_event_written` | 截断时写入 `fetch_events(event_type='fetch_count_limited')` | ✅ |

---

## Phase 1 横切测试回顾（11 tests）

### A1：重试分类（5 tests）

| 用例 | 验证点 | 状态 |
|---|---|---|
| `test_transient_failure_does_not_consume_attempt` | 瞬时错误 `attempt` 不自增 | ✅ |
| `test_permanent_failure_consumes_attempt` | 永久错误 `attempt+1` | ✅ |
| `test_permanent_failure_dead_letter` | 永久错误达 `max_attempts` → `failed` 死信 | ✅ |
| `test_transient_timeout_increments_consecutive` | 超时类瞬时错误 `consecutive_timeouts+1` | ✅ |
| `test_done_check_when_all_jobs_terminal` | 所有 job 终态后文章 → `done` | ✅ |

### B4：跨源近似去重（2 tests）

| 用例 | 验证点 | 状态 |
|---|---|---|
| `test_identical_vectors_trigger_dedup` | 相同 body 向量 → cosine distance=0 | ✅ |
| `test_different_content_not_dedup` | 不同内容 → 高距离，不触发去重 | ✅ |

### Pipeline 并发（4 tests）

| 用例 | 验证点 | 状态 |
|---|---|---|
| `test_enqueue_idempotent` | 重复入队 → 活跃态唯一 | ✅ |
| `test_pick_and_claim_skips_locked` | 领取后状态 → `running` | ✅ |
| `test_pick_and_claim_empty_queue` | 空队列 → `None` | ✅ |
| `test_priority_ordering` | `embed_core(1)` 先于 `summarize(2)` 被领 | ✅ |

---

## 修复的 Bug

| # | Bug | 修复 |
|---|---|---|
| 1 | `_classify_http_error` 已定义但未调用，401/403 被当瞬时错误走指数退避 | `_retry_transient` 内联 HTTP 状态码分类：401/403/400 → `PermanentError` |
| 2 | `EMBED_INSTRUCT_PREFIX` 硬导入 `app.llm.omlx`，facade 耦合 provider 实现 | 提升为 `OMLXProvider.embed_instruct_prefix` class attribute；Protocol 新增 `embed_instruct_prefix: str` |
| 3 | `worker.py` 单 LLMClient 信号量，embed 被 27B generate 阻塞 | 双 LLMClient（generate + embed 各自独立信号量） |
| 4 | Python except 块内 raise 的异常不被同 try 的其他 except 捕获 | 在 `httpx.HTTPStatusError` 分支内联分类逻辑，不调用 `_classify_http_error` 方法 |
