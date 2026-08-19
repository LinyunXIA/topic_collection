# 切片三测试报告

> 日期：2026-08-19
> 前置：切片一 48/48 + 切片二 12/12 已通过

---

## 测试结果总览

```
============================== 75 passed in 2.37s ==============================
```

| 类别 | 切片一 | 切片二 | 切片三新增 | 合计 |
|---|---|---|---|---|
| 集成测试（PRD 验收） | 22 | 8 | 12 | 42 |
| 单元测试 | 26 | 4 | 3 | 33 |
| **合计** | **48** | **12** | **15** | **75** |

**75 tests, 0 failures, 2.37s**

---

## PRD §15 验收覆盖

### 验收 3：主题跨源聚合（8 tests）

| 用例 | 验证点 | 状态 |
|---|---|---|
| `test_create_topic` | 创建主题 + keywords JSON 存储 | ✅ |
| `test_list_topics` | 列出所有主题 | ✅ |
| `test_update_topic` | 更新名称 + 关键词 | ✅ |
| `test_delete_topic` | 删除主题（CASCADE 清理） | ✅ |
| `test_keyword_hit` | 关键词快路径命中 | ✅ |
| `test_keyword_no_hit` | 关键词未命中 | ✅ |
| `test_match_writes_article_topics` | article_topics(method='keyword') 写入 | ✅ |
| `test_aggregate_filters_loser` | 聚合过滤 dedupe_of IS NULL | ✅ |

### 验收 5：Wiki 关键词搜索（4 tests）

| 用例 | 验证点 | 状态 |
|---|---|---|
| `test_generate_article_wiki` | 文章→wiki 词条（Markdown + related_json） | ✅ |
| `test_generate_wiki_upsert` | 重复生成 upsert（slug 唯一） | ✅ |
| `test_search_wiki` | wiki 关键词搜索命中 | ✅ |
| `test_slugify` | 标题→slug 转换 | ✅ |

### classify_topics LLM 慢路径（2 tests）

| 用例 | 验证点 | 状态 |
|---|---|---|
| `test_classify_with_fake_llm` | FakeLLM generate → parse → 写入 article_topics(method='llm') | ✅ |
| `test_classify_no_summary_skips` | 无 summary → 跳过 LLM 分类 | ✅ |

### 聚合查询（1 test）

| 用例 | 验证点 | 状态 |
|---|---|---|
| `test_aggregate_empty_topic` | 空主题返回空列表 | ✅ |
