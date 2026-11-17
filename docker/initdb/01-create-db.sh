#!/bin/bash
# MySQL 首次启动时执行（/docker-entrypoint-initdb.d/ 按文件名顺序运行 .sh / .sql）
# MYSQL_DATABASE 只会创建 Agent 库；中台库在这里补建，并按容器环境变量里的用户名授权，
# 因此修改 MYSQL_USER / PLATFORM_MYSQL_DATABASE 后不会出现「建库成功但没权限」。
set -euo pipefail

PLATFORM_DB="${PLATFORM_MYSQL_DATABASE:-platform}"
DB_USER="${MYSQL_USER:-sku}"

mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" <<SQL
CREATE DATABASE IF NOT EXISTS \`${PLATFORM_DB}\`
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
GRANT ALL PRIVILEGES ON \`${PLATFORM_DB}\`.* TO '${DB_USER}'@'%';
FLUSH PRIVILEGES;
SQL

echo "[initdb] 中台库 ${PLATFORM_DB} 已就绪，已授权给 ${DB_USER}@%"
