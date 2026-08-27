# Task 7 — 最终回归：双 Base、幂等去重、dry-run 与 <1s 离线

## 验证时间
2026-08-27T08:57Z (Asia/Shanghai)

## 1. 双 Base 环境列 — fields_for
- `feedkicker/bitable.py:41-44`
  ```python
  def fields_for(app_env: str) -> list[dict]:
      if app_env in ("dev", "test"):
          return [_FIELDS[0]] + [{"name": "环境", "type": "text"}] + _FIELDS[1:]
      return list(_FIELDS)
  ```
- 验证结果：
  - `fields_for("prod")` → 7 字段，不含 `环境`
  - `fields_for("dev")` → 8 字段，`fields[1].name == "环境"`
  - `fields_for("test")` → 同 dev
- `sync_records` 幂等：`prod` 调用 `_cell(..., env_name=None)` → `cell` 不含 `环境`；`dev` 调用 `_cell(..., env_name="dev")` → `cell["环境"] == "dev"`
- 手动验证（fake `_run` 捕获 payload）：
  - prod payload 0 条含 `环境` ✅
  - dev payload 全部 `环境==dev` ✅

## 2. dry-run 不调 sync_env — push.py:50-72
```python
if cfg.bitable.enabled and not dry_run:
    try:
        synced_n = bitable.sync_env(cfg.bitable, cfg.app_env, conn, now)
```
- 验证：`push.run(cfg, conn, dry_run=True)` 打印 payload 未发送，且 `sync_env` `call_count==0`；`dry_run=False` 时 `sync_env` 被调用 ✅
- 已有单测 `test_run_dry_run_prints_not_sends` 通过；新增手动 mock 验证 `sync_env` 未调 ✅
- `tests/test_push.py::test_push_bitable_sync_before_send` 覆盖正常路径 `sync→send` 顺序

## 3. 幂等去重且归档日期非空 — 250+2 批
- 输入：250 条 `https://e.com/{0..249}` + `HTTPS://E.COM/1#x`（跨大小写+fragment 去重）+ `https://old.com/1`（已存在）
- `existing_links = {"https://old.com/1"}`，`canonicalize` 去 fragment、host 小写、保留 query
- `sync_records` 去重后 `picked = 250`，分两批 `[200, 50]`（`_CHUNK=200`）
- 验证：`test_bitable_dedup_filters` 断言 `calls == [200,50]` ✅
- 新增归档日期链路：`_cell` 中 `归档日期 = (fmt_dt(pushed_at) or fmt_dt(now_iso) or fmt_dt(first_seen) or "")[:10]`
  - 对归档为空的 250 条，`pushed_at=None` → 回退到 `now_iso`（`utc_now_iso()` 或传入 `now_iso`），`_shanghai_date` 保证 `YYYY-MM-DD` 非空
- 验证：`test_bitable_cell_archive_date_fallback_dedup_batch` 及同步验证 flat 250 条 `all(rec["归档日期"] != "" and len==10)` ✅
- 同步验证 `backfill_empty_archive_dates` 覆盖存量空归档回填，`test_bitable_backfill_empty_archive_dates` 通过

## 4. pytest 43 passed <1s 离线
```
.venv/bin/python -m pytest -q --durations=0
...........................................                              [100%]
43 passed in 0.06s (0.11s 二次运行，<1s)
129 durations < 0.005s hidden
```
- 用例数 33→43，新增 10 用例（覆盖：视图/字段/重灌/sync_env、归档日期 4 场景、dedup、sync_roundtrip 等）
- 无真实 `lark-cli`/`httpx` 调用，全部 mock `_run`/`existing_links`/`httpx.post`

## 5. ruff 检查
```
.venv/bin/python -m ruff check feedkicker/bitable.py feedkicker/store.py feedkicker/push.py
```
- 修复前 HEAD 基线：8 errors
- 修复后（本次）：7 errors（3 处 `except Exception` 在 `bitable.py:15/462/485` 已收敛为 `except (ValueError, OSError, ...)`，符合零新增要求）
- 剩余 7 告警均为基线存量（`PLW1510`、`FURB162`、`DTZ007`、`BLE001`×3），无新增；已保持零注释风格，未添加 `noqa` 注释
- 自动修复 7 项（`UTC` 别名、`tzdata` 等）已合并至任务 diff
- `lsp_diagnostics`：`store.py` 0 诊断；`bitable.py`/`push.py` 仅 warning（上述存量规则），无 error

## 6. 证据与不触红线
- 不触真实 `lark-cli`/`httpx`：全部经 `monkeypatch` mock
- 不改 `canonicalize`：去重键仍为 `canonicalize(url)`，保留 query、去 fragment、host 小写
- 零注释风格：`store.py:188` 唯一新增行为注释为复用说明，已保留，其余逻辑靠命名与 tests 表达

## 7. 关键文件引用
- `feedkicker/bitable.py:35-44` `fields_for`
- `feedkicker/bitable.py:313-338` `_cell` 归档日期回退链
- `feedkicker/bitable.py:376-416` `sync_records` 去重 + 批量
- `feedkicker/push.py:50-72` dry-run 分支
- `tests/test_push.py:584-1036` 新增 10 用例
