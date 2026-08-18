#!/usr/bin/env bash
# pg_dump 备份脚本 — DESIGN §10
# 用法: bash scripts/backup.sh
# 输出: data/backups/tc-YYYYMMDD.sql.gz
# 保留: 默认 14 天，可通过 TC_BACKUP_RETENTION_DAYS 环境变量覆盖

set -euo pipefail

# ── 配置 ──────────────────────────────────────────────
BACKUP_DIR="${TC_BACKUP_DIR:-./data/backups}"
RETENTION_DAYS="${TC_BACKUP_RETENTION_DAYS:-14}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/tc-${TIMESTAMP}.sql.gz"

# Docker Compose 配置
COMPOSE_PROJECT="${TC_COMPOSE_PROJECT:-topic_collection}"
POSTGRES_SERVICE="${TC_POSTGRES_SERVICE:-postgres}"
DB_USER="${TC_DB_USER:-tc}"
DB_NAME="${TC_DB_NAME:-topic_collection}"

# ── 前置检查 ──────────────────────────────────────────
mkdir -p "${BACKUP_DIR}"

if ! command -v docker &>/dev/null; then
    echo "❌ docker 未安装" >&2
    exit 1
fi

if ! docker compose -p "${COMPOSE_PROJECT}" ps "${POSTGRES_SERVICE}" --status running --format json 2>/dev/null | grep -q .; then
    echo "❌ postgres 容器未运行" >&2
    exit 1
fi

# ── 执行备份 ──────────────────────────────────────────
echo "📦 开始备份: ${DB_NAME} → ${BACKUP_FILE}"

docker compose -p "${COMPOSE_PROJECT}" exec -T "${POSTGRES_SERVICE}" \
    pg_dump -U "${DB_USER}" -d "${DB_NAME}" \
    --no-owner --no-privileges --clean --if-exists \
    | gzip > "${BACKUP_FILE}"

BACKUP_SIZE=$(du -h "${BACKUP_FILE}" | cut -f1)
echo "✅ 备份完成: ${BACKUP_FILE} (${BACKUP_SIZE})"

# ── 清理过期备份 ──────────────────────────────────────
CLEANED=$(find "${BACKUP_DIR}" -name "tc-*.sql.gz" -mtime +"${RETENTION_DAYS}" -type f -print -delete | wc -l)
if [ "${CLEANED}" -gt 0 ]; then
    echo "🗑️  清理 ${CLEANED} 个超过 ${RETENTION_DAYS} 天的旧备份"
fi

# ── 列出当前备份 ──────────────────────────────────────
echo ""
echo "📁 当前备份:"
ls -lh "${BACKUP_DIR}"/tc-*.sql.gz 2>/dev/null | tail -5 || echo "  (无)"
