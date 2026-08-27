# fix-bitable-archive-date - Work Plan

## TL;DR (For humans)
<!-- Fill this LAST, after the detailed plan below is written, so it summarizes the REAL plan. -->

**What you'll get:** 飞书多维表格新增数据的“按日期”分组恢复正常：增量不再产生空归档日期，昨天/今天已产生的空值行被批量回填到正确上海日期，视图按归档日期倒序即刻可浏览。

**Why this approach:** 根因是“先归档后推送”时 `_cell` 只读 `pushed_at`（此时仍为 NULL），改为显式 `Asia/Shanghai` 并回退到本班 `now`；存量走 `record-update` 批量补写，不删表，保留 324 行历史与幂等去重语义。

**What it will NOT do:** 不复活已废弃的 sheets/gh-pages；不改表格字段类型（仍 text YYYY-MM-DD）；不调换抓取→归档→发卡→mark_pushed 顺序；不改 `canonicalize` 去重键。

**Effort:** Short — 约 1 天（改 3 文件 + 新增回填入口 + TDD 单测）
**Risk:** Low — 纯回退逻辑与批量更新，失败仅 WARNING 不阻断发卡；已加 tzdata 兜底与离线 mock 防线上误写
**Decisions to sanity-check:** ① 归档日期口径=归档当天（上海）pushed_at→now ② 存量=自动批量回填（按 推送时间→bitable_synced_at→first_seen）③ TDD 先补失败用例

Your next move: 运行 `gh issue create --body-file .omo/issue-fix-bitable-archive-date.md` 建 issue，然后 `$start-work fix-bitable-archive-date` 启动 worker；或先跑高精度 dual review 再开工。

---

> TL;DR (machine): Short/Low — 修正 _cell 上海回退 + 双 Base 批量回填 + TDD 覆盖，恢复按日期分组

## Scope
### Must have
- 增量归档日期不再为空：`_cell` 在 `pushed_at` 为空时回退到本班 `now`，统一 `ZoneInfo("Asia/Shanghai")` 转上海再取 `YYYY-MM-DD`，与 Base 创建 `time-zone Asia/Shanghai` 一致
- `store.select_unsynced` 扩展返回 `first_seen, bitable_synced_at`，供回退链与测试确定性；`sync_env`/`sync_records` 接收显式 `now_iso` 避免 `astimezone()` 漂移
- `push.py` 将本班 `now` 透传给 `bitable.sync_env`，保持 `抓取入库→写多维表格→推飞书→mark_pushed` 顺序不变，归档失败仍 `WARNING` 不阻断发卡
- 存量空值回填：新增 `python -m feedkicker.bitable --backfill`（或 `--fix-archive-date`）扫描 `归档日期` 为空的记录，按 `pushed_at → bitable_synced_at → first_seen` 回退生成上海日期，`≤200/批` `record-update` 批量写回，幂等跳过非空，`prod` 与 `dev-test` 双 Base 分别执行
- 视图自愈校验：`ensure_archive_date_field` + `create_date_view` 确保 `按日期: group 归档日期 desc + sort 推送时间 desc` 就绪，空组消失
- 回归不破：`existing_links` 去重、dry-run 不写表、`fields_for` 环境列区分、`<1s` 离线 33+ 用例约束保留

### Must NOT have (guardrails, anti-slop, scope boundaries)
- 不复活 `sheets_archive.py` / `publish.py` gh-pages（`site.enabled=false` 全环境，DESIGN §16/§17 已废弃）
- 不改 `归档日期` 字段类型为 datetime，不改 `canonicalize`（去 fragment、host 小写、保留 query）
- 不调换 `push.py` 编排顺序（`mark_pushed` 仍在发送成功后），不把归档改为推后
- 不引入常驻进程/队列/asyncio，不新增外部依赖除 `tzdata`（ZoneInfo 后备）
- 不对线上 Base 做 `purge_all_records` 全表清空重灌除非显式 `--reseed`；回填默认走 `record-update` 批量补写

## Verification strategy
> Zero human intervention - all verification is agent-executed.
- Test decision: **TDD** + `pytest` + `pytest-mock`/`monkeypatch`，全部离线（`httpx`/`subprocess` mock），`feedparser` 用本地 fixture，`sqlite :memory:`，`lark-cli` 全 mock `_run`
- Evidence: `.omo/evidence/task-<N>-fix-bitable-archive-date.md`（或 `attemptDir` 内同名），每个 todo 自带 happy + failure 场景与精确断言/命令
- 关键命令：
  - `.venv/bin/python -m pytest -q` 全量 `<1s`（含新增用例）
  - `.venv/bin/python -m pytest tests/test_push.py::test_bitable_cell_archive_date_fallback -q`
  - `.venv/bin/python -m pytest tests/test_push.py::test_store_select_unsynced_fields -q`
  - `ruff check feedkicker/bitable.py feedkicker/store.py feedkicker/push.py`
  - `python -m feedkicker.bitable --env dev --backfill --dry-run`（mock 验证，不碰线上）

## Execution strategy
### Parallel execution waves
> Target 5-8 todos per wave. Fewer than 3 (except the final) means you under-split.
- Wave 1: Todo 1 (TDD 用例先行) — 单独波，为后续实现提供失败保护
- Wave 2: Todo 2 + Todo 3 可并行（bitable _cell/时区 与 store 扩展无直接依赖）
- Wave 3: Todo 4 依赖 2+3（sync_env 串联 now）
- Wave 4: Todo 5 依赖 4（回填需新 sync 链路稳定） + Todo 6 可并行（视图校验独立）
- Wave 5: Todo 7 依赖 5+6（回归与双 Base 集成）

### Dependency matrix
| Todo | Depends on | Blocks | Can parallelize with |
| --- | --- | --- | --- |
| 1 | — | 2,3,4,5,7 | — |
| 2 | 1 | 4,5,7 | 3 |
| 3 | 1 | 4,5,7 | 2 |
| 4 | 2,3 | 5,7 | — |
| 5 | 4 | 7 | 6 |
| 6 | 1 | 7 | 5 |
| 7 | 5,6 | F1-F4 | — |

## Todos
> Implementation + Test = ONE todo. Never separate.
<!-- APPEND TASK BATCHES BELOW THIS LINE WITH edit/apply_patch - never rewrite the headers above. -->
- [ ] 1. TDD：补归档日期回退与上海时区切点失败用例
  What to do: 在 `tests/test_push.py` 新增 `test_bitable_cell_archive_date_fallback` 等 4 用例：① `pushed_at=None, now=2026-08-26T16:00:00Z → 归档日期=2026-08-27`（UTC 16:00 = 上海次日 00:00 跨日）② `pushed_at=2026-08-25T01:30:00Z → 归档日期=2026-08-25`（有值优先）③ `pushed_at=None, first_seen=2026-08-25T23:59:00Z, now=2026-08-26T00:01:00Z` 回退链验证 ④ `existing_links` 去重后仍保留归档日期；全部用 `monkeypatch` mock `_run`，不调真实 lark-cli
  Must NOT do: 不改生产代码，仅加测试；不引入真实网络/文件 Base 调用
  Parallelization: Wave 1 | Blocked by: — | Blocks: 2,3,4,5,7
  References (executor has NO interview context - be exhaustive): `feedkicker/bitable.py:307-328 _cell` `feedkicker/store.py:167-182 select_unsynced` `feedkicker/fetch.py:16-17 utc_now_iso` `tests/test_push.py:616-632 test_bitable_cell_fields` `docs/DESIGN.md:§18.1 归档日期 text` `feedkicker/bitable.py:101-114 create_base time-zone`
  Acceptance criteria (agent-executable): ` .venv/bin/python -m pytest tests/test_push.py::test_bitable_cell_archive_date_fallback -q` 失败（4 用例红），`grep -n "归档日期" tests/test_push.py` 命中新增用例
  QA scenarios (name the exact tool + invocation): happy: `pytest -q -k test_bitable_cell_archive_date_fallback` 断言 `cell["归档日期"]=="2026-08-27"`；failure: 传入 `pushed_at="invalid-iso"` 时回退到 now 不抛异常且为 `YYYY-MM-DD`，Evidence `.omo/evidence/task-1-fix-bitable-archive-date.md` 记录 pytest 输出
  Commit: N | test(bitable): add failing TDD for archive date fallback and Shanghai midnight

- [ ] 2. 修正 bitable._cell 归档日期推导为显式 Asia/Shanghai + now 回退
  What to do: 改 `feedkicker/bitable.py:307-328`：新增 `from zoneinfo import ZoneInfo` 与 `try: SHANGHAI=ZoneInfo("Asia/Shanghai") except: SHANGHAI=timezone(timedelta(hours=8))`；`fmt_dt` 改为 `dt.astimezone(SHANGHAI)`；`_cell(item, now_iso=None, env_name=None)` 签名新增 `now_iso`，`归档日期 = (fmt_dt(pushed_at) or fmt_dt(now_iso) or fmt_dt(item.get("first_seen")) or "")[:10]`，`pushed_at` 优先否则 `now_iso`；`sync_records` 同步改签名接收 `now_iso` 并传给 `_cell`；`pyproject.toml` 增加 `tzdata` 可选依赖或条件导入回退
  Must NOT do: 不改 `canonicalize`，不改 `fields_for` 结构，不把 `归档日期` 改为 datetime 类型
  Parallelization: Wave 2 | Blocked by: 1 | Blocks: 4,5,7 | Can parallelize with: 3
  References: `feedkicker/bitable.py:1-32 import/SHANGHAI` `feedkicker/bitable.py:307-328 _cell` `feedkicker/bitable.py:366-404 sync_records` `feedkicker/fetch.py:16-17 utc_now_iso` `pyproject.toml:dependencies`
  Acceptance criteria: ` .venv/bin/python -m pytest tests/test_push.py::test_bitable_cell_archive_date_fallback -q` 由红转绿 4/4 通过；`python -c "from feedkicker.bitable import _cell; print(_cell({'pushed_at':None,'url':'https://e.com/1'}, now_iso='2026-08-26T16:00:00Z')['归档日期'])"` 输出 `2026-08-27`
  QA scenarios: happy: `pytest -q -k test_bitable_cell_archive_date` 绿；failure: 若 `ZoneInfo` 不可用仍回退到 `+08` 不抛 `ZoneInfoNotFoundError`（mock ZoneInfo 抛错路径），Evidence `.omo/evidence/task-2-fix-bitable-archive-date.md`
  Commit: Y | fix(bitable): derive archive date via Asia/Shanghai fallback to now

- [ ] 3. 扩展 store.select_unsynced 返回 first_seen/bitable_synced_at 供回退
  What to do: 改 `feedkicker/store.py:167-182`：`SELECT feed_id, entry_key, title, url, description, published_at, pushed_at, first_seen, bitable_synced_at FROM articles WHERE bitable_synced_at IS NULL ORDER BY first_seen, feed_id`（避免 `ORDER BY pushed_at` NULLS FIRST 聚簇）；同步调整 `mark_synced` 双查询问题描述性注释；确保 `connect` 已含 `bitable_synced_at` 列（已有 42-44）；加 `test_store_select_unsynced_fields` 验证返回键包含新字段且不破坏现有 `select_pending` 语义
  Must NOT do: 不改 `select_pending` 排序语义，不改 `download`/`mark_pushed`，不新增索引
  Parallelization: Wave 2 | Blocked by: 1 | Blocks: 4,5,7 | Can parallelize with: 2
  References: `feedkicker/store.py:36-45 connect` `feedkicker/store.py:98-105 select_pending` `feedkicker/store.py:167-191 select_unsynced/mark_synced` `tests/test_push.py:574-581 test_store_sync_roundtrip`
  Acceptance criteria: `.venv/bin/python -m pytest tests/test_push.py::test_store_select_unsynced_fields -q` 通过；`python -c "import sqlite3; from feedkicker import store; c=store.connect(':memory:'); store.download(c,'F',[{...}],'2026-08-26T00:00:00Z'); print(store.select_unsynced(c)[0].keys())"` 含 `first_seen`/`bitable_synced_at`
  QA scenarios: happy: 新字段存在且 `ORDER BY first_seen` 正确；failure: 空表 `select_unsynced` 返回 `[]` 不抛错，Evidence `.omo/evidence/task-3-fix-bitable-archive-date.md`
  Commit: Y | fix(store): expose first_seen/bitable_synced_at for archive date fallback

- [ ] 4. 串联 push.now → bitable.sync_env/sync_records 并修复 mark_synced 双查询
  What to do: 改 `feedkicker/bitable.py:407-424 sync_env(bt, app_env, conn, now_iso=None)` 接收 `now_iso`，`unsynced = store.select_unsynced(conn)` 后 `env_name` 推导，`ok = sync_records(token, table_id, unsynced, env_name, now_iso)`，成功后 `store.mark_synced(conn, unsynced, now_iso or utc_now_iso())`（复用已查列表不二次查询）；改 `feedkicker/push.py:50-55` 将本班 `now` 传入 `bitable.sync_env(cfg.bitable, cfg.app_env, conn, now)`；`sync_records` 签名同步为 `sync_records(app_token, table_id, items, env_name, now_iso)` 并透传给 `_cell`
  Must NOT do: 不调换 `push.run` 顺序（仍先归档后发卡），归档异常仍仅 `WARNING` 不阻断，发卡失败仍保留 `bitable_synced_at` 已标记语义
  Parallelization: Wave 3 | Blocked by: 2,3 | Blocks: 5,7
  References: `feedkicker/push.py:19-57 run` `feedkicker/bitable.py:366-424 sync_records/sync_env` `feedkicker/store.py:167-190 select_unsynced/mark_synced` `tests/test_push.py:503-526 test_push_bitable_sync_before_send`
  Acceptance criteria: `.venv/bin/python -m pytest tests/test_push.py::test_push_bitable_sync_before_send -q` 通过且 mock 捕获 `sync_env` 的 `now_iso` 与 `push.now` 一致；`pytest -q` 全量绿
  QA scenarios: happy: `push.run` 中 `sync_env` 收到 `now` 且 `_cell` 生成非空归档日期；failure: `sync_records` 某批 `_run` 返回非 0 则 `sync_env` 抛 `RuntimeError` 且 `mark_synced` 未执行（保留待重试），Evidence `.omo/evidence/task-4-fix-bitable-archive-date.md`
  Commit: Y | fix(push,bitable): thread now through archiving to avoid empty archive date

- [ ] 5. 新增存量空值回填入口 --backfill（双 Base 批量 record-update）
  What to do: 在 `feedkicker/bitable.py` 新增 `backfill_empty_archive_dates(app_token, table_id, env_name=None, dry_run=False)`：分页 `record-list` 拉全量（`--json` 解析 `fields` 含 归档日期/推送时间/link），筛选 `归档日期 == ""` 的行，按 `推送时间 → bitable_synced_at → first_seen`（取自本地 `store.select` 关联或记录中 `推送时间`）→ 上海日期回退，`≤200/批` 调用 `base +record-update`（若 verb 为 `record-batch-update` 则适配）批量补写，返回修复数；CLI `python -m feedkicker.bitable --env prod --backfill [--dry-run]` 与 `--env dev` 分别对 `prod` 与 `dev-test` Base 执行，`--dry-run` 仅统计不写；先以 `lark-cli base --help` 验证 verb，若缺则回退为 `WARNING` 并提示 `--reseed`；增加 `purge`  guardrail 注释禁止默认全删
  Must NOT do: 默认不执行 `purge_all_records` 全删，不改 `existing_links` 去重键，不碰非空归档日期
  Parallelization: Wave 4 | Blocked by: 4 | Blocks: 7 | Can parallelize with: 6
  References: `feedkicker/bitable.py:255-289 purge_all_records` `feedkicker/bitable.py:331-363 existing_links` `feedkicker/bitable.py:366-404 sync_records` `feedkicker/bitable.py:426-465 __main__` `feedkicker/store.py:36-45 connect`
  Acceptance criteria: mock `_run`：`record-list` 返回 3 行含 2 空归档日期，`record-update` 被调用 1 批且 payload 含正确 `YYYY-MM-DD`；`python -m feedkicker.bitable --env dev --backfill --dry-run` 统计正确且不调 `record-update`
  QA scenarios: happy: 2 空行补齐为上海日期；failure: `_run` 对某批返回非 0 则整批不视为成功并 `WARNING` 保留重试，Evidence `.omo/evidence/task-5-fix-bitable-archive-date.md`
  Commit: Y | feat(bitable): backfill empty archive dates via batch update per Base

- [ ] 6. 视图与字段幂等校验及分组回归
  What to do: 确保 `ensure_archive_date_field` 对存量 Base 幂等补列，`setup_view`/`create_date_view` 校验 `按日期` 视图 `group 归档日期 desc + sort 推送时间 desc` 已存在（若缺则创建），新增 `test_bitable_views_grouping_not_empty`：mock `_run` 捕获 `view-set-group` 含 `归档日期` 且 `desc:true`，并断言新写入记录 `归档日期 != ""`
  Must NOT do: 不重建 `按来源` 视图，不改已有视图排序字段外配置
  Parallelization: Wave 4 | Blocked by: 1 | Blocks: 7 | Can parallelize with: 5
  References: `feedkicker/bitable.py:206-252 ensure_archive_date_field/create_date_view/setup_view` `docs/DESIGN.md:§18.1 视图` `tests/test_push.py:637-693 views/ensure field`
  Acceptance criteria: `.venv/bin/python -m pytest tests/test_push.py::test_bitable_views_grouping_not_empty -q` 通过
  QA scenarios: happy: 视图已存在时 `ensure_archive_date_field` 返回 True 不新建；failure: `view-list` 返回空时 `setup_view` 返回 False 并 `WARNING` 不抛错，Evidence `.omo/evidence/task-6-fix-bitable-archive-date.md`
  Commit: Y | fix(bitable): ensure date view grouping and archive field idempotent

- [ ] 7. 双 Base、幂等去重、dry-run 与 <1s 离线回归
  What to do: 补回归：① `test_bitable_sync_env_dual_base` 验证 `sync_records` 对 `prod` 不含 `环境` 列、`dev` 含 `环境==dev` ② `test_push_dry_run_not_sync` 验证 `push.run(dry_run=True)` 不调 `sync_env` ③ `test_bitable_dedup_preserves_archive_date` 验证 `existing_links` + `batch_seen` 去重不丢失归档日期（250+2 批场景）④ 全量 `pytest -q` 保持 `<1s`（`time` 统计），`ruff check` 零新增告警；更新 `docs/DESIGN.md §18` 补充归档日期回退链与回填入口说明（可选）
  Must NOT do: 不引入真实 `lark-cli`/`httpx` 调用，不改 `canonicalize`，不新增常驻
  Parallelization: Wave 5 | Blocked by: 5,6
  References: `feedkicker/bitable.py:35-39 fields_for` `feedkicker/push.py:50-57,69-72 dry-run` `tests/test_push.py:584-613 bitable_dedup` `tests/test_push.py:400-416 dry_run` `AGENTS.md:33 用例全离线，<1s`
  Acceptance criteria: `.venv/bin/python -m pytest -q` 通过且新增用例总数 ≥37，用时 `<1s`（`pytest --durations=0`）；`ruff check feedkicker/` 无新增 error
  QA scenarios: happy: 双 Base payload 环境列正确；failure: `existing_links` 抛错时 `sync_records` 仍对批内去重生效，Evidence `.omo/evidence/task-7-fix-bitable-archive-date.md`
  Commit: Y | test: dual Base, dedup and dry-run regression plus offline budget

## Final verification wave
> Runs in parallel after ALL todos. ALL must APPROVE. Surface results and wait for the user's explicit okay before declaring complete.
- [ ] F1. Plan compliance audit
- [ ] F2. Code quality review
- [ ] F3. Real manual QA
- [ ] F4. Scope fidelity

## Commit strategy
- 1 commit per todo (7 commits)，前缀 `fix`/`feat`/`test` 按 todo 标注，信息含 `fix-bitable-archive-date` 与关联路径
- 提交走 feature 分支 → PR → merge，不直推 main（AGENTS.md 约定）
- 回填执行另起一次性运维提交，不与代码提交混同
- 证据目录 `.omo/evidence/task-*-fix-bitable-archive-date.md` 随 PR 附可复核日志

## Success criteria
- 增量：`push` 新产生的多维表格行 `归档日期` 非空且等于上海 `YYYY-MM-DD`，跨日 00:00 切点正确
- 存量：`--backfill` 后 `按日期` 视图空组消失，昨天/今天记录按正确日期分组
- 视图：`按日期` 视图 `group 归档日期 desc + sort 推送时间 desc` 就绪，`prod` 与 `dev-test` 双 Base 均校验
- 回归：`existing_links` 去重、`dry-run` 不写表、发卡失败重试、20KB 降级、幂等标记均保持
- 测试：`pytest -q` 全离线 `<1s`，新增 TDD 用例全部通过，`ruff check` 无新增告警
- 运维：`python -m feedkicker.bitable --env prod --backfill --dry-run` 可预览，真实回填幂等可重入

