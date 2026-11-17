#!/usr/bin/env bash
# 无 docker compose 插件时的降级部署脚本：docker build + docker run
# 用法：在项目根目录执行 bash docker/deploy-no-compose.sh
set -euo pipefail
# 注意：本脚本是「无 compose 插件」时的 SQLite 兜底路径；MySQL / Redis / Qdrant 请用 docker-compose.deploy.yml

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
DATA_DIR="$ROOT/data"
mkdir -p "$DATA_DIR"

TOKEN="${PLATFORM_INTERNAL_TOKEN:-demo-internal-token}"
AUTH_SECRET="${AUTH_SECRET:-change-me-demo-secret}"
PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"

echo "==> 1/5 构建镜像（pip 源：$PIP_INDEX_URL）"
docker build -f docker/Dockerfile \
  --build-arg PIP_INDEX_URL="$PIP_INDEX_URL" \
  -t sku-agent:latest .

echo "==> 2/5 创建网络"
docker network inspect sku-agent-net >/dev/null 2>&1 || docker network create sku-agent-net

echo "==> 3/5 启动 Agent"
docker rm -f sku-agent-app >/dev/null 2>&1 || true
docker run -d --name sku-agent-app --network sku-agent-net --restart unless-stopped \
  --env-file .env \
  -e DATABASE_URL=sqlite:////app/data/sku_demo.db \
  -e REDIS_URL= \
  -e MCP_TRANSPORT=memory \
  -e PYTHONPATH=/app/src \
  -e PLATFORM_API_URL=http://platform:8001 \
  -e PLATFORM_INTERNAL_TOKEN="$TOKEN" \
  -e AUTH_SECRET="$AUTH_SECRET" \
  -e SCHEDULER_ENABLED="${SCHEDULER_ENABLED:-false}" \
  -e TZ=Asia/Shanghai \
  -v "$DATA_DIR:/app/data" \
  sku-agent:latest \
  sh -c 'python scripts/ensure_init.py; exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers'

echo "==> 4/5 启动数据中台（等待 Agent 初始化完成）"
docker rm -f sku-agent-platform >/dev/null 2>&1 || true
docker run -d --name sku-agent-platform --network sku-agent-net --restart unless-stopped \
  --env-file .env \
  -e PLATFORM_DB_URL=sqlite:////app/data/platform/platform.db \
  -e PLATFORM_INTERNAL_TOKEN="$TOKEN" \
  -e TZ=Asia/Shanghai \
  -v "$DATA_DIR:/app/data" \
  sku-agent:latest \
  sh -c 'while [ ! -f /app/data/sku_demo.db ]; do sleep 2; done; python scripts/ensure_init.py --platform; exec uvicorn data_platform.api.main:app --host 0.0.0.0 --port 8001 --proxy-headers'

echo "==> 5/5 启动 Nginx（等待两个服务健康）"
for i in $(seq 1 120); do
  if docker exec sku-agent-app python -c "import httpx; httpx.get('http://127.0.0.1:8000/api/health', timeout=2)" >/dev/null 2>&1 \
     && docker exec sku-agent-platform python -c "import httpx; httpx.get('http://127.0.0.1:8001/api/v1/health', timeout=2)" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

docker rm -f sku-agent-nginx >/dev/null 2>&1 || true
docker run -d --name sku-agent-nginx --network sku-agent-net --restart unless-stopped \
  -p 80:80 -p 500:500 \
  -v "$ROOT/docker/nginx.conf:/etc/nginx/conf.d/default.conf:ro" \
  nginx:1.27-alpine

echo "完成：http://<服务器IP>/  （数据中台：:500/api/v1，需 X-Internal-Token）"
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
