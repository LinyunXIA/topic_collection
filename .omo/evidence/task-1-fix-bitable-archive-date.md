# Task 1 证据 — 飞书多维表格归档日期空值 bug TDD 红阶段

## 目标
为 `feedkicker/bitable.py:_cell` 归档日期空值 bug 补 TDD 失败用例，仅加测试不改生产代码，运行应为红。

## 变更
- 仅修改 `tests/test_push.py`，在 `test_bitable_cell_fields` 之后新增 4 个 `test_bitable_cell_archive_date_fallback_*` 用例 + 1 个参数化 `test_bitable_cell_archive_date_fallback`（4 场景），保持零注释、monkeypatch/mock、不触真实 lark-cli/httpx。

## 覆盖场景
1. `pushed_at=None, now_iso="2026-08-26T16:00:00Z"` → 期望 `"2026-08-27"`，显式 `ZoneInfo("Asia/Shanghai")` 验证跨日切点
2. `pushed_at="2026-08-25T01:30:00Z", now_iso="2026-08-27T00:00:00Z"` → 仍为 `"2026-08-25"`（有值优先），并断言 `now_iso in sig.parameters`
3. `pushed_at=None, first_seen="2026-08-25T23:59:00Z", now_iso=None` → 回退链期望 `"2026-08-26"`（上海 07:59 次日）
4. `existing_links` 去重：250+2 批含重复 canonical，`sync_records` 后 payload 中归档日期均非空且 `len==10`

## Pytest 红阶段（必现 4 failed）

### `::test_bitable_cell_archive_date_fallback` 精确命名（要求命令）
```
$ .venv/bin/python -m pytest tests/test_push.py::test_bitable_cell_archive_date_fallback -q
FFFF                                                                     [100%]
FAILED tests/test_push.py::test_bitable_cell_archive_date_fallback[cross_midnight] - assert '' == '2026-08-27'
FAILED tests/test_push.py::test_bitable_cell_archive_date_fallback[pushed_at_priority] - assert 'now_iso' in sig.parameters
FAILED tests/test_push.py::test_bitable_cell_archive_date_fallback[first_seen_chain] - assert '' == '2026-08-26'
FAILED tests/test_push.py::test_bitable_cell_archive_date_fallback[dedup_batch] - assert False (all 归档日期 != "")
4 failed in 0.08s
```
> 符合要求：`::test_bitable_cell_archive_date_fallback` 4 failed（≥2 亦满足）

### `-k test_bitable_cell_archive_date_fallback` 全量匹配
```
$ .venv/bin/python -m pytest -k test_bitable_cell_archive_date_fallback -q
FFFFFFFF                                                                 [100%]
FAILED test_bitable_cell_archive_date_fallback_cross_midnight - assert '' == '2026-08-27'
FAILED test_bitable_cell_archive_date_fallback_pushed_at_priority - assert 'now_iso' in sig.parameters
FAILED test_bitable_cell_archive_date_fallback_first_seen_chain - assert '' == '2026-08-26'
FAILED test_bitable_cell_archive_date_fallback_dedup_batch - assert False
FAILED test_bitable_cell_archive_date_fallback[cross_midnight] - assert '' == '2026-08-27'
FAILED test_bitable_cell_archive_date_fallback[pushed_at_priority] - assert 'now_iso' in sig.parameters
FAILED test_bitable_cell_archive_date_fallback[first_seen_chain] - assert '' == '2026-08-26'
FAILED test_bitable_cell_archive_date_fallback[dedup_batch] - assert False
8 failed, 33 deselected in 0.12s
```
> 4 个独立函数 + 4 个参数化场景 = 8，独立函数同样满足命名要求。

### 原有用例仍离线 <1s
```
$ .venv/bin/python -m pytest -k "not test_bitable_cell_archive_date_fallback" -q
.................................                                        [100%]
33 passed, 8 deselected in 0.05s

$ .venv/bin/python -m pytest -q
8 failed, 33 passed in 0.14s
```
> 33 个原有用例保持通过、离线 <1s，未引入真实网络。

## Grep 命中
```
$ grep -n "test_bitable_cell_archive_date_fallback" tests/test_push.py
634:def test_bitable_cell_archive_date_fallback_cross_midnight(monkeypatch):
653:def test_bitable_cell_archive_date_fallback_pushed_at_priority(monkeypatch):
670:def test_bitable_cell_archive_date_fallback_first_seen_chain(monkeypatch):
688:def test_bitable_cell_archive_date_fallback_dedup_batch(monkeypatch):
714:def test_bitable_cell_archive_date_fallback(monkeypatch, scenario):

$ grep -n "归档日期" tests/test_push.py
623:    assert cell["归档日期"] == "2026-08-25" or len(cell["归档日期"]) == 10
648:    assert cell["归档日期"] == "2026-08-27"
649:    assert cell["归档日期"] == expected
650:    assert len(cell["归档日期"]) == 10
665:    assert cell["归档日期"] == "2026-08-25"
666:    assert cell["归档日期"] == expected
667:    assert len(cell["归档日期"]) == 10
683:    assert cell["归档日期"] == "2026-08-26"
684:    assert cell["归档日期"] == expected
685:    assert len(cell["归档日期"]) == 10
708:    assert all(rec["归档日期"] != "" for rec in flat)
709:    assert all(len(rec["归档日期"]) == 10 for rec in flat)
...
```

## 约束核验
- 未改 `feedkicker/` 生产代码：`git diff -- feedkicker/` 无输出
- 未改 `canonicalize`/`fields_for`，未引入新依赖，`pyproject.toml` 未动
- 未写真实 `lark-cli` 调用：全部 `monkeypatch.setattr(bitable, "_run", ...)` / `existing_links`
- 跨日用例显式 `ZoneInfo("Asia/Shanghai")`，未用 `astimezone()`
- 断言精确：`cell["归档日期"] == "YYYY-MM-DD"` 且 `len==10`
- 位置：紧跟 `test_bitable_cell_fields`（634 行起）附近

## 复现命令
```
.venv/bin/python -m pytest tests/test_push.py::test_bitable_cell_archive_date_fallback -v
.venv/bin/python -m pytest -k test_bitable_cell_archive_date_fallback -q
```
均应为红，证明 TDD 红阶段成功。
