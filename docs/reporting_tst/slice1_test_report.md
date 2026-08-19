# 切片一测试报告

> 日期：2026-08-19
> Python：3.14.6 · pytest：9.1.1 · SQLAlchemy：2.0.52 · asyncpg：0.31.0
> DB：PostgreSQL 17 + pgvector 0.8.6（Docker, port 5433）
> LLM：FakeLLMProvider（内存 mock，固定 1536 维向量 + JSON fixture）

---

## 测试结果总览

```
============================== 48 passed in 1.40s ==============================
```

| 类别 | 用例数 | 通过 | 失败 | 耗时 |
|---|---|---|---|---|
| 集成测试（PRD 验收） | 22 | 22 | 0 | ~0.8s |
| 单元测试 | 26 | 26 | 0 | ~0.6s |
| **合计** | **48** | **48** | **0** | **1.40s** |

---

## PRD §15 验收覆盖

### 验收 1：建库 + 抓取 + 清洗（8 tests）

| 用例 | 验证点 | 状态 |
|---|---|---|
| `test_extensions_loaded` | `CREATE EXTENSION vector` 已执行 | ✅ |
| `test_vector_dimension` | `article_embeddings.vector` 列维度 = 1536 | ✅ |
| `test_url_hash_dedup` | sha256(canonical_url) 幂等 + 不同 URL → 不同 hash | ✅ |
| `test_content_hash_dedup` | sha256 归一化（空白压缩） | ✅ |
| `test_article_insert_and_unique` | 文章入库 + `url_hash` UNIQUE + `ON CONFLICT DO NOTHING` 幂等 | ✅ |
| `test_clean_article` | trafilatura HTML→文本提取，`is_parseable=True`，`word_count>0` | ✅ |
| `test_clean_article_unparseable` | 空 HTML + 空标题 → `is_parseable=False` | ✅ |
| `test_language_detection` | lingua 检测 en/zh（长文本 ≥10 字） | ✅ |

### 验收 7：中文摘要（3 tests）

| 用例 | 验证点 | 状态 |
|---|---|---|
| `test_complete_summarize_hook` | summaries upsert + `content_hash` 版本守卫 + key_points_json + confidence | ✅ |
| `test_run_summarize_with_fake_llm` | FakeLLM generate → parse_with_repair → complete_summarize 全链路 | ✅ |
| `test_summary_tsv_refresh` | 摘要落库后 `articles.tsv` 包含中文关键词（两阶段刷新） | ✅ |

### 验收 8：关键词全文搜索（3 tests）

| 用例 | 验证点 | 状态 |
|---|---|---|
| `test_keyword_search` | jieba 切词 + `websearch_to_tsquery` + `ts_rank` 排序命中 | ✅ |
| `test_keyword_search_no_match` | 英文文章 + 中文搜索词 → 不误匹配 | ✅ |
| `test_match_keywords` | 关键词快路径：命中主题写入 `article_topics(method='keyword')` | ✅ |

---

## 端到端 Pipeline 测试（3 tests）

| 用例 | 验证点 | 状态 |
|---|---|---|
| `test_enqueue_and_complete_flow` | enqueue → FakeLLM summarize → summary 落库 + `embed_summary` 入队 | ✅ |
| `test_embed_core_flow` | FakeLLM embed → complete_embed → title + body 两条向量落库 | ✅ |
| `test_near_dedup_no_hit` | 语义不同的文章（量子物理 vs 意大利烹饪）不触发 cosine 近似去重 | ✅ |

---

## FakeLLM Provider 测试（5 tests）

| 用例 | 验证点 | 状态 |
|---|---|---|
| `test_healthcheck` | `healthy=True`, models 列表非空 | ✅ |
| `test_generate_returns_json` | 返回 `summary_zh` JSON fixture, `finish_reason='stop'` | ✅ |
| `test_embed_dim` | 1536 维固定向量, `len(embeddings)==1` | ✅ |
| `test_rerank` | Cohere 风格返回 indices + scores | ✅ |
| `test_call_count` | 调用计数正确递增 | ✅ |

---

## 单元测试（26 tests）

### TestDedup（6 tests）

| 用例 | 状态 |
|---|---|
| `test_url_hash_deterministic` — 同 URL → 同 hash | ✅ |
| `test_url_hash_different_urls` — 不同 URL → 不同 hash | ✅ |
| `test_url_hash_canonicalizes` — 去 fragment / lowercase / 去 trailing slash | ✅ |
| `test_content_hash_deterministic` — sha256 hex 64 位 | ✅ |
| `test_content_hash_normalizes_whitespace` — 多空格归一 | ✅ |
| `test_content_hash_different_content` — 不同内容 → 不同 hash | ✅ |

### TestCleaner（6 tests）

| 用例 | 状态 |
|---|---|
| `test_extract_content` — HTML→文本提取 | ✅ |
| `test_clean_article_removes_script` — `<script>` 标签去除 | ✅ |
| `test_clean_article_unparseable` — 空内容 → `is_parseable=False` | ✅ |
| `test_detect_language_en` — 英文检测 → `"en"` | ✅ |
| `test_detect_language_zh` — 中文检测 → `"zh"` | ✅ |
| `test_detect_language_short_returns_unknown` — 短文本 → `"unknown"` | ✅ |

### TestStructured（7 tests）

| 用例 | 状态 |
|---|---|
| `test_parse_json_valid` — 正常 JSON 解析 | ✅ |
| `test_extract_json_from_markdown` — 从 ` ```json ``` ` 代码块提取 | ✅ |
| `test_repair_json_trailing_comma` — 尾逗号修复 | ✅ |
| `test_repair_json_single_quotes` — 单引号→双引号修复 | ✅ |
| `test_parse_with_repair_valid` — expected_keys 校验通过 | ✅ |
| `test_parse_with_repair_garbage` — 乱码 → `None` | ✅ |
| `test_parse_with_repair_list_not_dict` — JSON 数组 → `None`（非 dict） | ✅ |

### TestFTS（2 tests）

| 用例 | 状态 |
|---|---|
| `test_jieba_join` — 中文切词 + 空格拼接 | ✅ |
| `test_jieba_join_mixed` — 中英混合切词 | ✅ |

### TestPrompts（3 tests）

| 用例 | 状态 |
|---|---|
| `test_get_prompt_summarize` — summarize 模板返回 system + user | ✅ |
| `test_get_prompt_no_keyerror` — **关键**：summarize 不触发 classify_topics 的 KeyError（惰性求值修复） | ✅ |
| `test_get_prompt_unknown_task` — 未知任务 → `ValueError` | ✅ |

### TestConfig（2 tests）

| 用例 | 状态 |
|---|---|
| `test_load_settings` — `vector_dim=1536`, DSN 含 `topic_collection` | ✅ |
| `test_task_priority` — `embed_core=1`, `summarize=2`, `embed_summary=6` | ✅ |

---

## 开发期间修复的 Bug

| # | Bug | 修复 |
|---|---|---|
| 1 | `get_prompt` dict 字面量立即求值 → summarize 触发 classify_topics 的 `KeyError` | 改为先查模板再 `.format()` |
| 2 | `:vec::vector` SQL 语法被 asyncpg 误解析 | 改为 `CAST(:vec AS vector)` |
| 3 | `complete_embed` 缺少 `job` 参数 → `NameError` | 添加 `job: dict | None = None` |
| 4 | Alembic `env.py` 使用 asyncpg DSN 报 `MissingGreenlet` | 改用同步驱动 `psycopg2` |
| 5 | GIN 索引 `postgresql.GIN()` 不存在 | 改用 `postgresql_using="gin"` |
| 6 | `atttypmod - 8` 维度校验错误 | pgvector atttypmod 直接=维度 |
| 7 | Worker 无 task_handler → 只领取不处理 | 注册 `task_dispatcher` 分发器 |
| 8 | pytest asyncpg "attached to a different loop" | `asyncio_default_fixture_loop_scope = "session"` |

---

## 真实环境验证（非 pytest）

Worker 消费 Hacker News 20 篇文章的实际运行数据：

| 指标 | 值 |
|---|---|
| 文章入库 | 20 篇（HN frontpage） |
| 语言检测 | 19 en, 1 de |
| embed_core 完成 | 20/20（title + body 向量，40 条 embedding） |
| summarize 完成 | 18/20（2 篇 LLM 返回格式异常） |
| embed_summary 完成 | 部分（受 summarize 影响） |
| LLM 调用 | oMLX `Qwen3.8-27B-MLX-4bit` + `Qwen3-Embedding-8B-4bit-DWQ` |
| 单篇 summarize 耗时 | ~20-60s |
| 单篇 embed 耗时 | ~0.5s |
