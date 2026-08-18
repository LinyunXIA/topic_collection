.PHONY: init worker backup

# 初始化数据库（CREATE EXTENSION vector + alembic upgrade head）
init:
	python -m scripts.init_db

# 启动 worker（Phase 1 单进程：worker + APScheduler）
worker:
	python -m app.worker

# 备份数据库
backup:
	bash scripts/backup.sh

# 运行测试
test:
	pytest -x -v

# 代码检查
lint:
	ruff check app/ tests/
