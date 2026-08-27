---
slug: fix-bitable-archive-date
status: awaiting-approval
intent: clear
review_required: false
pending-action: write .omo/plans/fix-bitable-archive-date.md
approach: 修复 Bitable 归档日期空值导致按日期分组失效 - 修正 _cell 归档日期推导逻辑（pushed_at 为空时回退到归档当天 Asia/Shanghai），对齐 push.py 先归档后推送的编排，并提供存量空值行的回填路径
---

# Draft: fix-bitable-archive-date

## Components (topology ledger)
<!-- Lock the SHAPE before depth. One row per top-level component that can succeed or fail independently. -->
<!-- id | outcome (one line) | status: active|deferred | evidence path -->
| id | outcome | status | evidence |
| --- | --- | --- | --- |
| C1 | 增量归档日期正确写入 - 新条目归档日期不再为空 | active | feedkicker/bitable.py:307-328 _cell, feedkicker/push.py:50-57 sync ordering, feedkicker/store.py:167-182 select_unsynced |
| C2 | 存量空值回填 - 昨天/今天已入表的空归档日期被补齐 | active | feedkicker/bitable.py:264-289 purge_all_records / sync_records, data/tc-prod.sqlite3 (bitable_synced_at) |
| C3 | 时区一致性 - 归档日期按 Asia/Shanghai 而非服务器本地/UTC 切日 | active | feedkicker/bitable.py:308-315 fmt_dt astimezone(), feedkicker/bitable.py:101-114 create_base time-zone Asia/Shanghai |
| C4 | 视图分组自愈 - 按日期视图按归档日期倒序正确分组 | active | feedkicker/bitable.py:223-252 create_date_view, docs/DESIGN.md:§18.1-§18.2 |
| C5 | 回归不破现有推送/去重语义 - 归档失败不阻断发卡、幂等去重保留 | active | feedkicker/push.py:50-57, feedkicker/bitable.py:366-404 existing_links/sync_records |
| C6 | 单测覆盖 - 归档日期回退逻辑与编排顺序有可执行断言 | active | tests/test_push.py:616-632 test_bitable_cell_fields, tests/test_push.py:711-742 sync_env |

## Open assumptions (announced defaults)
<!-- Record any default you adopt instead of asking, so the user can veto it at the gate. -->
<!-- assumption | adopted default | rationale | reversible? -->
| assumption | adopted default | rationale | reversible? |
| --- | --- | --- | --- |
| 修复后归档日期应保持向后兼容的文本 YYYY-MM-DD 形态 | 保持 text 字段与 YYYY-MM-DD 格式 | DESIGN §18.1 定义为 text，保持视图分组兼容 | yes |
| push 顺序保持 抓取入库→写多维表格→推飞书→mark_pushed 不变 | 不调换顺序，仅修 _cell 回退 | AGENTS.md/ DESIGN §6 编排顺序不可调换 | yes |

## Findings (cited - path:lines)
- `feedkicker/bitable.py:307-328` _cell: `归档日期: (fmt_dt(pushed_at) or "")[:10]` - pushed_at 为空时直接置空，未回退；fmt_dt 内部用 `datetime.fromisoformat(...).astimezone()` 依赖服务器本地时区而非显式 Asia/Shanghai
- `feedkicker/push.py:50-57` 编排：`pending = select_pending()` 后 `bitable.sync_env()` 先于 `mark_pushed(pending, now)` - 此时 select_unsynced 拉到的条目 pushed_at 仍为 NULL，故 _cell 必然空；成功路径的 sync_records 在 pushed_at 仍空时写入
- `feedkicker/store.py:167-182` select_unsynced: `SELECT ... pushed_at ... WHERE bitable_synced_at IS NULL ORDER BY pushed_at` - 未取 first_seen/bitable_synced_at，_cell 无法回退到 first_seen
- `feedkicker/bitable.py:407-423` sync_env: 写入后 `mark_synced(conn, select_unsynced(conn), utc_now_iso())` 重新查询标记，归档日期却在写入时已定为空，标记无法回填
- `docs/DESIGN.md:§18.1-§18.2` 结构：字段增加 归档日期(text)，视图②按日期 group 归档日期倒序·推送时间倒序；v0.5 验收记录 2026-08-25 重灌 266+60 条成功，说明历史重灌时 pushed_at 已存在故当时有值
- `tests/test_push.py:616-632` test_bitable_cell_fields: 仅覆盖 pushed_at 有值时归档日期 == 2026-08-25，未覆盖 pushed_at 为空回退场景，当前未暴露 bug
- `feedkicker/bitable.py:35-39` fields_for: prod 与 dev-test 共享 Base 但 dev-test 多 环境列，修复需同时覆盖两 Base
- `feedkicker/bitable.py:206-221` ensure_archive_date_field: 已可自动补列，但空值行修复需走 record-update 而非仅补列

## Decisions (with rationale)
- 采用 CLEAR 路由：用户描述现象与预期分组已明确，剩余仅是推导口径/回填范围/时区等 owner-decision forks
- 分类 Standard：涉及 bitable.py / push.py / store.py / tests 4-5 文件，需数据迁移与视图校验，非 Trivial
- Q1 归档日期口径决议：归档当天(上海) - pushed_at 有值取 pushed_at 日期，无值回退到 sync 的 now，转为 Asia/Shanghai 再取 YYYY-MM-DD；与 Base 时区与视图分组一致
- Q2 存量回填决议：自动批量回填 - 新增回填路径扫描空归档日期记录，按 pushed_at→bitable_synced_at→first_seen 回退补齐，不删表，prod 与 dev-test 双 Base 均覆盖
- Q3 测试策略决议：TDD - 先为归档日期回退、跨日切点、幂等去重补失败单测，再修实现，保证 33 用例仍 <1s 离线
- 时区决议：显式 ZoneInfo("Asia/Shanghai") 转换，不再依赖 astimezone() 服务器本地时区

## Scope IN
- 增量写入修复：_cell 归档日期在 pushed_at 为空时回退到归档当天（显式 Asia/Shanghai），入表即有值
- 存量修复：为昨天/今天已产生的空归档日期提供一键回填（按推送时间或 bitable_synced_at 回填 YYYY-MM-DD）
- 时区显式化：归档日期切日统一用 Asia/Shanghai，与 Base 创建时 --time-zone Asia/Shanghai 一致
- select_unsynced / sync_records 链路补齐 now 回退参数或 first_seen 兜底，保证测试可确定性
- 视图校验：确保 按日期 视图 group 归档日期、sort 推送时间 已就绪
- 回归单测：新增 pushed_at 为空回退、跨日切点、幂等去重不丢失归档日期的覆盖

## Scope OUT (Must NOT have)
- 不复活已废弃的 sheets_archive / gh-pages(site.enabled=false) 链路
- 不改动多维表格字段类型（保持 text YYYY-MM-DD，不改 datetime）
- 不调整 push.py 编排顺序（不把 mark_pushed 提前到归档前，仅修归档侧回退）
- 不新增常驻进程/队列/异步化
- 不改 canonicalize 去重键规则（保留 query、host 小写、去 fragment）

## Open questions
- 已全部决议：Q1=归档当天(上海)、Q2=自动批量回填、Q3=TDD

## Approval gate
status: awaiting-approval
approach: 增量 _cell 回退到 Asia/Shanghai 归档当天 + 存量 batch-update 回填 + 显式时区 + TDD 单测
next-action: write .omo/plans/fix-bitable-archive-date.md
pending-action: write .omo/plans/fix-bitable-archive-date.md
<!-- 已就绪，等待用户显式 approve 后写 plan -->

