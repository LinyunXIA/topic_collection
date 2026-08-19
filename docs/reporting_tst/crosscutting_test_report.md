# 横切测试报告

> 日期：2026-08-19
> 前置：切片一~三 86/86 已通过（见 slice1/2/3_test_report.md）
> 覆盖：Phase 1 横切（A1 重试分类 + B4 近似去重 + Pipeline 并发）+ Phase 1+（fetch --count）

---

## 测试结果总览

```
============================== 15 passed in 0.83s ==============================
```

| 类别 | Phase 1 横切 | Phase 1+ 新增 | 合计 |
|---|---|---|---|
| A1：重试分类 | 5 | 0 | 5 |
| B4：跨源近似去重 | 2 | 0 | 2 |
| Pipeline 并发 | 4 | 0 | 4 |
| P1+.2：fetch --count | 0 | 4 | 4 |
| **合计** | **11** | **4** | **15** |

**15 tests, 0 failures, 0.83s**

---

## A1：重试分类（5 tests）

| 用例 | 验证点 | 状态 |
|---|---|---|
| `test_transient_failure_does_not_consume_attempt` | 瞬时错误（连接拒绝）→ `error_class='transient'`、`attempt` 不自增、回 `queued` | ✅ |
| `test_permanent_failure_consumes_attempt` | 永久错误（JSON 解析失败）→ `error_class='permanent'`、`attempt+1` | ✅ |
| `test_permanent_failure_dead_letter` | 连续 3 次永久错误 → `status='failed'` 死信 | ✅ |
| `test_transient_timeout_increments_consecutive` | 超时类瞬时错误 → `consecutive_timeouts+1` | ✅ |
| `test_done_check_when_all_jobs_terminal` | 所有 job 终态 → `articles.status='done'` | ✅ |

---

## B4：跨源近似去重（2 tests）

| 用例 | 验证点 | 状态 |
|---|---|---|
| `test_identical_vectors_trigger_dedup` | 相同 body 向量 → cosine distance=0，同粒度匹配 | ✅ |
| `test_different_content_not_dedup` | 不同内容（量子物理 vs 意大利烹饪）→ 高距离，不触发去重 | ✅ |

---

## Pipeline 并发（4 tests）

| 用例 | 验证点 | 状态 |
|---|---|---|
| `test_enqueue_idempotent` | 重复入队 → `ON CONFLICT DO NOTHING`，活跃态 `queued` 唯一 | ✅ |
| `test_pick_and_claim_skips_locked` | `FOR UPDATE SKIP LOCKED` 领取后状态 → `running` | ✅ |
| `test_pick_and_claim_empty_queue` | 空队列 → `pick_and_claim` 返回 `None` | ✅ |
| `test_priority_ordering` | `embed_core(priority=1)` 先于 `summarize(priority=2)` 被领 | ✅ |

---

## P1+.2：`tc feeds fetch --count N`（4 tests）

| 用例 | 验证点 | 状态 |
|---|---|---|
| `test_items_truncated_to_count` | 20 条 items + `count=5` → `items[:5]` 截断 | ✅ |
| `test_count_none_no_truncation` | `count=None` 时不截断，保持原列表 | ✅ |
| `test_count_larger_than_items_no_truncation` | `count > len(items)` 时不截断 | ✅ |
| `test_fetch_count_limited_event_written` | 截断时写入 `fetch_events(event_type='fetch_count_limited', item_count=N)` | ✅ |
