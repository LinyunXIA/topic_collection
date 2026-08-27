# [Bug] 飞书多维表格「归档日期」空值导致按日期分组失效

## 现象
- 预期：多维表格「文章」表按 `归档日期` 分组（`按日期` 视图：group 归档日期倒序 · 推送时间倒序）展示每日归档
- 实际：仅 `2026-08-25` 分组正常，`2026-08-26`（昨天）与 `2026-08-27`（今天）新数据 `归档日期` 均为**空值**，全部归入空组

## 根因
- `feedkicker/bitable.py:324`：`归档日期 = (fmt_dt(pushed_at) or "")[:10]`，`pushed_at` 为空即空
- `feedkicker/push.py:50-57` 编排：`sync_env` 在 `mark_pushed` 之前，`select_unsynced` 拉到的是 `pushed_at=NULL` 的待推条目，故增量写入必然空
- `feedkicker/store.py:167-182`：`select_unsynced` 未提供 `first_seen/bitable_synced_at`，无法回退
- 时区：`fmt_dt` 用 `astimezone()` 依赖服务器本地时区，非显式 `Asia/Shanghai`，与 Base `time-zone Asia/Shanghai` 不一致

## 影响
- 按日期视图失效，日报归档不可用
- 历史重灌（2026-08-25 324 行）因 `pushed_at` 已存在而正常，掩盖增量缺陷

## 修复方向（已规划）
- 增量：`_cell` 增加显式 `Asia/Shanghai` 转换，`pushed_at` 有值用它，无值回退到 `push.run` 的 `now`
- 存量：批量回填空 `归档日期`（按 `推送时间→bitable_synced_at→first_seen` 回退，不删表）
- 双 Base：`prod` 与 `dev-test` 分别回填
- 测试：TDD 补单测覆盖空回退与跨日切点

## 复现步骤
1. 抓取入库产生待推条目（`pushed_at IS NULL`）
2. 触发 `python -m feedkicker.push --env prod`（或 launchd 8:30/16:00）
3. 查看多维表格「按日期」视图，新行 `归档日期` 为空

## 验收标准
- 增量新行 `归档日期` 非空且等于上海日期 `YYYY-MM-DD`
- 存量空值行回填后不再归空组
- `pytest -q` 33+ 新增用例 <1s 离线，无真实 `lark-cli`/`httpx` 调用

---
自动生成于 `fix-bitable-archive-date` 规划，关联 `docs/DESIGN.md §18`、`feedkicker/bitable.py:307-328`、`feedkicker/push.py:50-57`、`feedkicker/store.py:167-182`

## 提交命令
```bash
gh issue create --title "[Bug] 飞书多维表格「归档日期」空值导致按日期分组失效" --body-file .omo/issue-fix-bitable-archive-date.md --label bug
# 或手动：gh issue create --repo LinyunXIA/topic_collection --title "..." --body-file ...
```
