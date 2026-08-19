# 切片二测试报告

> 日期：2026-08-19
> 前置：切片一 48/48 已通过（见 slice1_test_report.md）

---

## 测试结果总览

```
============================== 60 passed in 1.72s ==============================
```

| 类别 | 切片一 | 切片二新增 | 合计 |
|---|---|---|---|
| 集成测试（PRD 验收） | 22 | 8 | 30 |
| 单元测试 | 26 | 4 | 30 |
| **合计** | **48** | **12** | **60** |

**60 tests, 0 failures, 1.72s**

---

## PRD §15 验收 9 覆盖：混合检索

### RRF 融合单元测试（4 tests）

| 用例 | 验证点 | 状态 |
|---|---|---|
| `test_rrf_merge_basic` | 两通道重叠 → RRF 分数叠加，重叠项排名靠前 | ✅ |
| `test_rrf_merge_empty` | 空输入 → 空结果 | ✅ |
| `test_rrf_merge_single_channel` | 单通道正常排序 | ✅ |
| `test_rrf_merge_limit` | limit 截断正确 | ✅ |

### 关键词搜索集成测试（2 tests）

| 用例 | 验证点 | 状态 |
|---|---|---|
| `test_keyword_hit` | jieba 切词 + websearch_to_tsquery 命中中文文章 | ✅ |
| `test_keyword_no_match` | 英文文章 + 中文搜索词 → 不匹配 | ✅ |

### 语义搜索集成测试（2 tests）

| 用例 | 验证点 | 状态 |
|---|---|---|
| `test_semantic_search_with_embeddings` | embed_query → pgvector cosine → 命中 | ✅ |
| `test_semantic_empty_when_no_embeddings` | 无向量 → 空结果 | ✅ |

### 混合搜索集成测试（4 tests）

| 用例 | 验证点 | 状态 |
|---|---|---|
| `test_hybrid_mode` | 关键词+语义双通道 → RRF 融合命中 | ✅ |
| `test_keyword_only_mode` | `mode=keyword` 纯 FTS | ✅ |
| `test_empty_query` | 空查询 → 空结果 | ✅ |
| `test_fallback_to_keyword_when_no_llm` | 无 LLM → hybrid 自动降级 keyword | ✅ |

---

## 实现要点

| 组件 | 文件 | 关键设计 |
|---|---|---|
| 混合检索 | `app/services/search.py` | `search(q)` = 语义 top-k ∪ 关键词 top-k → RRF `1/(k+rank)` k=60 |
| 语义通道 | `_semantic_search()` | `embed_query`（instruct prefix）→ pgvector cosine，三粒度去重取最高分 |
| 关键词通道 | `_keyword_search()` | jieba → `websearch_to_tsquery` + `ts_rank`，过滤 `dedupe_of IS NULL` |
| 降级 | search() | LLM 不可用 → hybrid→keyword；语义异常 → hybrid→keyword |
| CLI | `app/services/cli.py` | `tc search --mode hybrid\|semantic\|keyword`（默认 hybrid） |
