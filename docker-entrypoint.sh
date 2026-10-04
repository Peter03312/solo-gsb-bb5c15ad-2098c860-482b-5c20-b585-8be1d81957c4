#!/bin/sh
# 容器入口：先幂等初始化 SQLite 数据库，再启动服务。
set -e

echo "[entrypoint] 初始化数据库: ${DB_PATH:-/data/sop.db}"
python -m app.init_db

echo "[entrypoint] 启动服务: ${APP_HOST:-0.0.0.0}:${APP_PORT:-8080}"
exec "$@"
