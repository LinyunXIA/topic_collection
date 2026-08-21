"""测试环境隔离 — 强制 TC_APP_ENV=test，避免 pytest 污染 dev（5433）

所有测试共享同一进程，conftest 在 collection 前执行，
setdefault 保证：
- 直接 `pytest` / `pytest tests/ -q` 默认走 test 库 5434/topic_collection_test
- `TC_APP_ENV=test make test` / `TC_APP_ENV=test pytest` 显式指定亦兼容
- `TC_APP_ENV=dev pytest` 显式指向 dev 时不强制覆盖（尊重用户意图）

DESIGN §5.4：dev 5433 / test 5434 / prod 5432 三环境隔离
"""

import os

# 默认走 test；已显式设置 TC_APP_ENV 则尊重原值（dev/test/prod）
os.environ.setdefault("TC_APP_ENV", "test")
