.PHONY: init worker prod prod-init backup

# 初始化数据库（CREATE EXTENSION vector + alembic upgrade head）
# 默认 dev（5433 tc/tc），prod 用 TC_APP_ENV=prod POSTGRES_PASSKEY=*** make prod-init
init:
	python -m scripts.init_db

prod-init:
	TC_APP_ENV=prod python -m scripts.init_db

# 启动 worker（Phase 1 单进程：worker + APScheduler）
worker:
	python -m app.worker

# 启动生产 worker（本机 postgres 5432 postgres/${POSTGRES_PASSKEY}，DESIGN §5.4.1）
prod:
	TC_APP_ENV=prod python -m app.worker

# 备份数据库
backup:
	bash scripts/backup.sh

# 运行测试
test:
	pytest -x -v

# 代码检查
lint:
	ruff check app/ tests/
