# Docker 部署指南（Ubuntu 22.04，仅使用 80 / 443 / 500 端口）

## 0. 端口与架构

| 服务器端口 | 用途 | 内部服务 |
|---|---|---|
| **80** | Web 看板 + Agent API（推荐访问入口） | nginx → `app:8000` |
| **500** | 数据中台 REST API（需 `X-Internal-Token`，可选，建议只对内网开放） | nginx → `platform:8001` |
| **443** | **https → http 兜底跳转**（自签证书，握手后 301 到 http）；需要真 HTTPS 时用 `docker-compose.https.yml` 覆盖为正式证书（见第 6 节） | nginx（仅跳转） |

- **compose 部署默认用 MySQL**（`DATABASE_URL` / `PLATFORM_DB_URL` 指向容器内 mysql，`depends_on: service_healthy` 保证建库完成再起 app）；
- 想退回零依赖的 SQLite + fakeredis（本地开发/无 Docker）：不起 compose，直接 `PYTHONPATH=src uvicorn app.main:app`；
- MySQL/Redis 不对外暴露端口，无需在服务器开放；
- 数据持久化在宿主机 `/opt/sku-agent/data/`（Agent 库、中台库、知识库、中台原始快照）；
- 首次启动自动初始化数据（Agent 300 SKU、数据中台 ERP/CRM/订单模拟源），重启不会重复初始化、不丢数据。
- **首次启动前必须先生成 443 兜底证书**（否则 nginx 因缺证书起不来）：
  ```bash
  cd /opt/sku-agent
  bash scripts/gen_redirect_cert.sh          # 生成 docker/certs/redirect-selfsigned.pem|.key（不入库）
  ```
  证书有效期 10 年、含 IP SAN；换服务器 IP 后重跑脚本（先删旧文件）即可。

> 说明：**Demo 默认关闭定时巡检**（`SCHEDULER_ENABLED=false`）。开启后 Agent 进程内会每小时 + 每日 09:00 真实调用 LLM 巡检，无人访问时也会持续消耗 token；手动全量巡检（看板「批量巡检」/ `POST /api/patrol`）不受影响。需要按原样演示定时能力时，把 `.env` 的 `SCHEDULER_ENABLED` 改成 `true` 再 `docker compose up -d app`，或用 `POST /api/scheduler/run-once?job=daily` 单次演练。

---

## 1. 服务器准备（Ubuntu 22.04）

```bash
sudo apt update && sudo apt install -y ca-certificates curl
# 安装 Docker 与 compose 插件（官方脚本）
curl -fsSL https://get.docker.com | sudo sh
sudo systemctl enable --now docker
docker --version && docker compose version

# 放行端口（如果启用了 ufw）
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow 500/tcp
```

> ⚠️ 国内/腾讯云服务器访问 `get.docker.com` 可能被重置（`curl: (35) Connection reset by peer`）。
> 若失败，改用腾讯云镜像源安装（见下方 1.1），Docker 版本与组件一致。

### 1.1 国内网络安装 Docker（腾讯云/阿里云镜像源）

```bash
# 先确认镜像源可达（返回 200 或 302 即可）
curl -I https://mirrors.cloud.tencent.com/docker-ce/linux/ubuntu/dists/jammy/InRelease
# 若腾讯云源不可达，改用：https://mirrors.aliyun.com/docker-ce/linux/ubuntu

sudo apt update
sudo apt install -y ca-certificates curl gnupg lsb-release

sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://mirrors.cloud.tencent.com/docker-ce/linux/ubuntu/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://mirrors.cloud.tencent.com/docker-ce/linux/ubuntu \
$(. /etc/os-release && echo $VERSION_CODENAME) stable" \
| sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
docker --version && docker compose version
```

配置 Docker 镜像加速（拉取 python/nginx 镜像更快）：

```bash
sudo mkdir -p /etc/docker
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{ "registry-mirrors": ["https://mirror.ccs.tencentyun.com"] }
EOF
sudo systemctl restart docker
```

---

## 2. 本地打包（Windows PowerShell）

`.venv`、数据库、缓存不要上传。在**项目上级目录**执行：

```powershell
cd <项目上级目录>
tar -czf sku-agent.tar.gz `
  --exclude=sku-agent-demo/.venv `
  --exclude=sku-agent-demo/.pytest_cache `
  --exclude=sku-agent-demo/data/sku_demo.db `
  --exclude=sku-agent-demo/data/platform `
  --exclude='sku-agent-demo/**/__pycache__' `
  sku-agent-demo
```

上传代码与 `.env`（Key 不要提交/打包进代码）：

```powershell
scp sku-agent.tar.gz root@<服务器IP>:/opt/
scp sku-agent-demo\.env root@<服务器IP>:/opt/sku-agent-env
```

> 也可以不上传 `.env`，第 3 节直接在服务器创建。

---

## 3. 服务器初始化

```bash
cd /opt
sudo tar -xzf sku-agent.tar.gz
sudo mv sku-agent-demo sku-agent
cd /opt/sku-agent
sudo mkdir -p data

# 方式 A：使用上传的 .env
sudo mv /opt/sku-agent-env .env   # 若上传过
# 方式 B：在服务器创建 .env（推荐）
cat > .env <<'EOF'
DEEPSEEK_API_KEY=sk-你的Key
DEEPSEEK_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-flash
AUTH_SECRET=请替换为 openssl rand -hex 32 的随机值
PLATFORM_INTERNAL_TOKEN=请替换为随机值
EOF

chmod 600 .env
```

生成随机密钥示例：

```bash
openssl rand -hex 32   # 粘贴到 .env 的 AUTH_SECRET / PLATFORM_INTERNAL_TOKEN
```

> **`.env` 的放置位置（务必确认）**：compose 的 `${VAR}` 变量替换只读「命令行 `--env-file` 指定的文件」或「compose 文件所在目录的 `.env`（即 `docker/.env`）」，
> 而 `env_file:` 只负责把变量**注入容器**、不参与替换。因此 `.env` 必须满足下列之一：
> ① 保持在**仓库根**（`/opt/sku-agent/.env`），启动时显式带 `--env-file .env`（本文档第 4 节及后续命令都已包含）；
> ② 或放到 `docker/.env`（与 compose 文件同目录），此时**去掉**命令里的 `--env-file .env`。
> 若两者都不做，容器会静默使用 compose 里的默认值（`AUTH_SECRET` 会退回公开默认值，见第 9 节安全清单）。
> 自检方式：`curl -s http://127.0.0.1/api/health | grep -o '"security":{[^}]*}'` ——
> `secret_is_default` / `platform_token_is_default` 都应显示 `false`。

---

## 4. 启动（HTTP，80 + 500）

```bash
cd /opt/sku-agent
# 关键：--env-file .env 让 compose 的 ${VAR} 替换读到仓库根的 .env
docker compose --env-file .env -f docker/docker-compose.deploy.yml up -d --build

# 查看状态与日志（首次初始化约 1~2 分钟）
docker compose --env-file .env -f docker/docker-compose.deploy.yml ps
docker compose --env-file .env -f docker/docker-compose.deploy.yml logs -f app
docker compose --env-file .env -f docker/docker-compose.deploy.yml logs -f platform
```

> 默认不启动 Redis 容器（`REDIS_URL` 默认空，应用走进程内 fakeredis）。需要真实 Redis 时：
> `docker compose --env-file .env -f docker/docker-compose.deploy.yml --profile redis up -d`，并在 `.env` 中设置 `REDIS_URL=redis://redis:6379/0`。

首次启动流程：
1. `app`：执行 `scripts/ensure_init.py` —— 按**表与行数**判断（库空才灌数据），需要时生成 300 SKU + 订单样例 + 知识库索引 → 启动 Agent；
2. `platform`：等 MySQL 健康检查通过 → 执行 `scripts/ensure_init.py --platform`（复用 `platform_bootstrap`：模拟源→入湖→dbt 转换）→ 启动中台；
3. `nginx`：两个服务健康后开始监听 80/500。

---

## 4.1 可选：MySQL / Redis / Qdrant / 腾讯 Embedding（4C4G 内存预算）

`docker-compose.deploy.yml` 的默认栈是「**MySQL** + Redis（可选，默认不启动容器）+ **Qdrant** + **腾讯 TokenHub embedding**」；组件不可用时会自动降级为「内存向量 + 哈希 embedding」，想要彻底零依赖就显式覆盖 `VECTOR_STORE=memory` / `EMBEDDING_BACKEND=hash`（关系库再覆盖成 `DATABASE_URL=sqlite:////app/data/sku_demo.db`）。每个组件都能单独关掉且自动降级：

```bash
# 仓库根目录的 .env（启动时用 --env-file .env 传给 compose；或放到 docker/.env）
# —— 这两个位置二选一，见第 3 节说明；只写进 docker/.env 时不必加 --env-file
# 关系库：默认已指向容器内 mysql，只需要改密码；下面两行是可选的显式写法
MYSQL_ROOT_PASSWORD=<随机>
MYSQL_USER=sku
MYSQL_PASSWORD=<随机>
DATABASE_URL=mysql+pymysql://sku:<随机>@mysql:3306/sku_demo?charset=utf8mb4
PLATFORM_DB_URL=mysql+pymysql://sku:<随机>@mysql:3306/platform?charset=utf8mb4
# 退回零依赖 SQLite（不想给 MySQL 分内存时）
# DATABASE_URL=sqlite:////app/data/sku_demo.db
# PLATFORM_DB_URL=sqlite:////app/data/platform.db
# 真实 Redis（可选）：设了就启用；并带 --profile redis 启动 redis 容器
# REDIS_URL=redis://redis:6379/0
VECTOR_STORE=qdrant
QDRANT_URL=http://qdrant:6333
EMBEDDING_BACKEND=tencent
TENCENT_EMBEDDING_API_KEY=sk-REPLACE_WITH_YOUR_KEY
TENCENT_EMBEDDING_BASE_URL=https://tokenhub.tencentmaas.com/v1
TENCENT_EMBEDDING_MODEL=kinfra-text-embedding-0.6b
```

> **未配置 `TENCENT_EMBEDDING_API_KEY` 时**：embedding 自动降级为哈希向量（512 维），向量集合随之变为 `kb_chunks_512`（集合名带维度后缀），检索走 Qdrant；若希望完全不用 Qdrant，把 `VECTOR_STORE=memory` 即可。

要点说明：

1. **MySQL 首次要建两个库**（Agent + 中台）：`docker/initdb/01-create-db.sh` 会在容器首次启动时创建 `platform` 库，并对 `MYSQL_USER` 授权（用户名取了环境变量，改用户名不会丢授权）；Agent 库由 `MYSQL_DATABASE` 创建。若已有 `data/mysql` 卷，改密码需同时改库内用户或清卷重建。
2. **初始化不再看文件**：`app`/`platform` 启动命令改为 `python scripts/ensure_init.py`（按"表 + 行数"判断，SQLite/MySQL 通用；容器重启不会重复灌 300 SKU）。
3. **镜像**：`mysql:8.0.39`（不要 8.4，`mysql_native_password` 默认禁用容易连不上）、`redis:7.4-alpine`、`qdrant/qdrant:v1.12.4`；国内先配 `registry-mirrors`（见第 1 节）。
4. **内存**（估算，以 `docker stats --no-stream` 实测为准）：MySQL 350–450MB、Redis 20–40MB、Qdrant 100–200MB、app 300–450MB、platform 150–250MB（分项合计约 0.9–1.4GB）；`mem_limit` 上限已写在 compose（mysql 768m / redis 192m / qdrant 384m / app 1536m / platform 512m，合计约 3.3GB），4G 机器装得下并建议再加 2GB swap 兜底。
5. **TokenHub**：需先在腾讯云开通「大模型服务平台 TokenHub」并创建 API Key；接口地域不匹配会 401/超时（大陆用 `tokenhub.tencentmaas.com`，新加坡用 `tokenhub-intl.tencentmaas.com`）；若开了 IP 白名单，把服务器出口 IP 加进去（否则 403 `CodeIPNotAllowed`）；429 会自动按 `Retry-After` 重试。
6. **验证**：
   ```bash
   curl -s http://127.0.0.1/api/health | python -m json.tool | head -40   # database/cache/vector_store/embedding
   docker compose --env-file .env -f docker/docker-compose.deploy.yml exec app python scripts/verify_stack.py
   docker compose --env-file .env -f docker/docker-compose.deploy.yml exec app python scripts/eval_rag.py
   ```
7. **降级演练**（可现场演示）：`docker stop sku-agent-qdrant` → 检索仍可用（退回内存，`/api/health` 标 degraded）；`docker stop sku-agent-redis` → 对话仍可用（缓存当 miss）；恢复后重启 app 即回到正常态。
8. **备份**：除 `data/sku_demo.db` / `data/platform`，切 MySQL 后还要备份 `data/mysql`、`data/qdrant`、`data/redis`（或用 `mysqldump`）。


## 5. 验证与访问

```bash
curl -s http://127.0.0.1/api/health            # Agent：应返回 {"status":"ok",...}
curl -s http://127.0.0.1/api/metrics | head    # 公开只读指标（匿名）
curl -s -H "X-Internal-Token: 你的PLATFORM_INTERNAL_TOKEN" \
     http://127.0.0.1:500/api/v1/health          # 数据中台健康检查
```

浏览器访问：

- **http://你的服务器IP/**：Web 看板（登录账号：`admin/hhw123`、`worker1/hhw111`、`worker2/hhw222`）
- **http://你的服务器IP/about.html**：项目简介（Markdown 渲染）
- 数据中台 REST：`http://你的服务器IP:500/api/v1/*`，请求需带 `X-Internal-Token`（仅建议联调/演示用）

建议先登录管理员，在页面确认指标卡、单 SKU 研判、AI 助手、批量巡检均正常，再对外演示。

---

## 6. HTTPS：默认只做「https → http」兜底，正式 HTTPS 用覆盖文件

### 6.0 默认行为（推荐先了解）

浏览器有时会默认以 `https://IP` 打开，而本项目默认只监听 80。为此 `docker/nginx.conf` 额外监听 **443**，
握手成功后**立刻 301 到 http**（`return 301 http://$host$request_uri`），用户点一次「继续访问（不安全）」
就回到 http 首页，不需要手动改地址。

- 证书由 `scripts/gen_redirect_cert.sh` 生成自签证书（`docker/certs/redirect-selfsigned.*`，已 `.gitignore`）；
- **首次启动前必须先生成**，否则 nginx 因缺证书无法启动：
  ```bash
  cd /opt/sku-agent && bash scripts/gen_redirect_cert.sh
  ```
- 自签证书只用于这一次握手，不存在密钥分发问题；想彻底消除告警就用下面的正式 HTTPS。

### 6.1 有正规证书（推荐，真正启用 HTTPS）

把证书放到服务器 `docker/certs/` 目录（与兜底证书共存，文件名不同）：

```bash
cd /opt/sku-agent
mkdir -p docker/certs
# 将 fullchain.pem 与 privkey.pem 上传/拷贝到 docker/certs/
chmod 600 docker/certs/privkey.pem
```

或用 certbot 申请（需域名解析到服务器）：

```bash
sudo apt install -y certbot
sudo certbot certonly --manual --preferred-challenges dns -d your.domain.com
# 把 /etc/letsencrypt/live/your.domain.com/{fullchain.pem,privkey.pem} 复制到 docker/certs/
```

### 6.2 无域名，先自签证书测试

```bash
cd /opt/sku-agent
mkdir -p docker/certs
openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
  -keyout docker/certs/privkey.pem \
  -out docker/certs/fullchain.pem \
  -subj "/CN=你的服务器IP"
chmod 600 docker/certs/privkey.pem
```

### 6.3 启用 HTTPS 覆盖

```bash
docker compose --env-file .env -f docker/docker-compose.deploy.yml -f docker/docker-compose.https.yml up -d --build
```

启用后：80 自动 301 到 443；`https://你的服务器IP/` 可访问看板；500 仍为 HTTP（中台内部接口）。

---

## 7. 日常运维

```bash
cd /opt/sku-agent
# 状态 / 日志
docker compose --env-file .env -f docker/docker-compose.deploy.yml ps
docker compose --env-file .env -f docker/docker-compose.deploy.yml logs -f --tail=200 app nginx platform

# 重启
docker compose --env-file .env -f docker/docker-compose.deploy.yml restart

# 停止 / 启动
docker compose --env-file .env -f docker/docker-compose.deploy.yml down
docker compose --env-file .env -f docker/docker-compose.deploy.yml up -d
```

### 数据备份（重要）

```bash
cd /opt/sku-agent
# 默认 MySQL 栈：备份 mysql 数据卷 + 向量库/Redis 卷 + 知识库
tar czf backup-$(date +%F-%H%M).tar.gz data/mysql data/qdrant data/redis data/kb
# 若显式切回 SQLite：把数据库文件与中台库一起打进去
# tar czf backup-$(date +%F-%H%M).tar.gz data/sku_demo.db data/platform data/kb
```

### 重置演示数据（会重新生成 300 SKU 与中台数据）

```bash
cd /opt/sku-agent
# 默认 MySQL 栈：清数据卷后重启，ensure_init.py 会按"表 + 行数"重新灌数据
docker compose --env-file .env -f docker/docker-compose.deploy.yml down -v
docker compose --env-file .env -f docker/docker-compose.deploy.yml up -d
# 若显式切回 SQLite：rm -f data/sku_demo.db && rm -rf data/platform，再 restart app platform
```

### 更新代码（scp 覆盖，保留数据）

```powershell
# 本地重新打包（同第 2 节），然后
scp sku-agent.tar.gz root@<服务器IP>:/opt/
```

```bash
cd /opt
mkdir -p /opt/backup && cp -r sku-agent/data /opt/backup/data-$(date +%F-%H%M)
sudo tar -xzf sku-agent.tar.gz
sudo rsync -a --delete sku-agent-demo/src/ sku-agent/src/
sudo rsync -a --delete sku-agent-demo/web/ sku-agent/web/
sudo rsync -a --delete sku-agent-demo/scripts/ sku-agent/scripts/
sudo rsync -a --delete sku-agent-demo/docker/ sku-agent/docker/
sudo rsync -a --delete sku-agent-demo/docs/ sku-agent/docs/
sudo cp sku-agent-demo/AGENTS.md sku-agent/AGENTS.md
sudo cp sku-agent-demo/requirements*.txt sku-agent/
cd sku-agent
docker compose --env-file .env -f docker/docker-compose.deploy.yml up -d --build
```

---

## 8. 常见问题

| 现象 | 排查 |
|---|---|
| 80/443/500 被占用 | `sudo ss -lntp \| grep -E ':80\|:443\|:500'`，停掉占用进程或改映射 |
| 页面打不开 | `docker compose ... logs app`；healthcheck 是否 healthy；nginx 日志 `docker logs sku-agent-nginx` |
| 中台 500 端口 502 | 平台还在首次初始化（看 `logs platform`），等待 healthcheck 变 healthy |
| AI 助手报错 | 检查 `.env` 的 `DEEPSEEK_API_KEY`；容器网络能否访问 `api.deepseek.com` |
| 依赖安装失败/构建慢 | 重新执行 `up -d --build`；国内网络可先配置 Docker 镜像加速 |
| 数据想保留 | 不要删除 `data/` 目录；重建容器不影响已挂载数据 |
| 想关闭定时巡检（默认已关闭） | `.env` 保持/改成 `SCHEDULER_ENABLED=false` 后重启 app；确认：`curl http://127.0.0.1/api/health` 中 `scheduler_enabled=false` |
| 想临时开启定时巡检 | `.env` 改 `SCHEDULER_ENABLED=true` → `docker compose --env-file .env -f docker/docker-compose.deploy.yml up -d app` |
| 只想单次演练定时任务（不常驻） | 用管理员登录后 `POST /api/scheduler/run-once?job=daily`，或看板批量巡检 |

---

## 9. 安全建议（上线前）

1. **修改默认密钥**：`AUTH_SECRET`、`PLATFORM_INTERNAL_TOKEN` 必须换随机值
   （`AUTH_SECRET` 是 HMAC 签名密钥：不换 = 任何人可伪造任意账号的 token）。
   应用启动时会做默认值校验：仍为公开默认值时会打 `WARNING` 并在 `/api/health.security` 中标记
   `secret_is_default: true` / `platform_token_is_default: true`，便于上线检查；**这一项仍以部署清单为准**；
2. `.env` 权限 `chmod 600`，不要进代码仓库/镜像；**放置位置见第 3 节**（仓库根 + `--env-file .env`，或 `docker/.env`）：放错位置会静默退回 compose 默认值，等于没改密钥；
3. 500 端口只暴露给需要联调的网络；正式环境建议不开放（Agent 已通过内网访问中台）；
4. 账号密码为演示账号，正式使用请改为企业 SSO 或接入统一认证并修改 `src/app/auth.py`；
5. compose 默认已用 MySQL；本地/无 Docker 场景才需要 `.env` 里显式设 `DATABASE_URL=sqlite:///...`（Redis 仍可留空走 fakeredis）。
6. 基础安全响应头已默认下发（`X-Content-Type-Options` / `X-Frame-Options` / `Referrer-Policy` / CSP）。
   CSP 当前保留 `'unsafe-inline'`（`web/` 的页面里有内联 `<script>` 与 style 属性），token 存 localStorage；
   收紧路径已明确：把内联脚本外置 + token 改 HttpOnly Cookie，即可去掉 `'unsafe-inline'`（列入生产部署清单的下一步）。

### 9.1 多副本 / 多 worker 前必须满足

| 前置条件 | 原因 | 不满足的后果 |
|---|---|---|
| `REDIS_URL` 指向共享 Redis | 限流计数、并发闸门、档案 CAS 短锁都在进程内 | 实际额度 × 副本数；同一份档案可能被两个副本同时写 |
| `CHECKPOINT_BACKEND=redis` | `data/checkpoints.sqlite` 是单写者（`/api/health.checkpoint.warning` 会提示） | 并发写排队、极端情况 "database is locked" |
| `SCHEDULER_ENABLED` 只在一个副本开启 | 每个进程都会起一个 APScheduler | 同一小时被 N 份定时巡检重复执行 |

`/api/health.runtime` 会把 `in_process_state` 与 `multi_replica_requirements` 直接列出来，
排障时不用猜「这个计数到底是全局的还是本进程的」。

### 9.2 接入第二个 Agent（中台治理下沉后）

中台已经能做「按调用方授权 / 限流 / 审计」，所以接新 Agent 不用改任何业务代码，只发一把 key：

```bash
# .env（中台侧）
PLATFORM_API_KEYS='{"sku-agent":{"key":"k-sku-xxxx","scopes":["read","write","approve"],"departments":null},"bi-agent":{"key":"k-bi-yyyy","scopes":["read"],"departments":["女装"],"rate_limit":120}}'
PLATFORM_RATE_LIMIT_PER_MINUTE=600
```

新 Agent 侧只需要：`PLATFORM_AGENT_ID=bi-agent`、`PLATFORM_INTERNAL_TOKEN=k-bi-yyyy`。
之后可以在中台直接查它的调用情况：

```bash
curl -H "X-Internal-Token: <ops-key>" "http://127.0.0.1:8001/api/v1/audit?agent_id=bi-agent&limit=20"
```

要点：
- **scope 分权**：只读 Agent 拿 `read` 就够了，`POST /erp/replenish-orders`、审批类接口会直接 403；
- **部门作用域**：`departments:["女装"]` 的 Agent 读男装 SKU 会 403，审批列表自动过滤并标注 `scope_dropped`；
- **配额按调用方**：一个 Agent 刷爆配额不影响其它 Agent；
- **审计可追责**：每行记录 `agent_id / path / status / request_id / decision`，Agent 侧 `run_detail.steps[].request_id` 能与它对上。

**也可以不重启中台就注册**（推荐，用 `PLATFORM_INTERNAL_TOKEN`（兼容全权身份）或 `PLATFORM_API_KEYS` 里任意带 `admin` scope 的 key 调）：

```bash
# 1) 注册（明文令牌只在这一次返回）
curl -X POST http://127.0.0.1:8001/api/v1/agents \
  -H "X-Internal-Token: <admin-key>" -H "Content-Type: application/json" \
  -d '{"agent_id":"bi-agent","scopes":["read"],"departments":["女装"],"rate_limit":120}'

# 2) 自检自己的权限（Agent 启动时建议调一次）
curl -H "X-Internal-Token: k-返回的令牌" http://127.0.0.1:8001/api/v1/agents/me

# 3) 改权限 / 轮换令牌 / 禁用（都是运行时生效，写操作会立即失效认证快照）
curl -X PATCH http://127.0.0.1:8001/api/v1/agents/bi-agent \
  -H "X-Internal-Token: <admin-key>" -H "Content-Type: application/json" \
  -d '{"scopes":["read","etl"]}'
curl -X POST http://127.0.0.1:8001/api/v1/agents/bi-agent/rotate -H "X-Internal-Token: <admin-key>"
curl -X POST http://127.0.0.1:8001/api/v1/agents/bi-agent/disable -H "X-Internal-Token: <admin-key>"
```

新 Agent 的能力清单也由中台给出：`GET /api/v1/metadata/capabilities`（工具分组 / 所需 scope / 部门作用域种类 / 落哪些端点）。
契约版本在响应头 `X-Platform-Contract`，Agent 侧 `PLATFORM_CONTRACT_STRICT=true` 时版本不符会直接报
`error_kind=contract` 而不是带着错字段继续跑。

多渠道验收（4 个 Agent 同时在线，含跨部门越权探测）：

```bash
python scripts/bench_multi_agent.py --agents 4 --turns 20
```

### 9.3 数据源：中台可以自己起

`PLATFORM_SOURCE_DB` 默认**空**——中台用自带的 `source_spec.py` 生成演示目录并合成 ERP/CRM/订单快照，
**不读 Agent 演示库**。这样容器里（没有 `data/sku_demo.db`、默认 MySQL）也能自举出完整的 mart。
只有在本地想复用 Agent 演示数据时才显式设 `PLATFORM_SOURCE_DB=/app/data/sku_demo.db`

---

## 10. 容量与压测（4C4G 单机）

对话链路的并发行为可以复现，不用"感觉还行"：

```bash
# mock 模型、真实链路（鉴权→会话→记忆→限流→并发闸门→grounding），零成本
PYTHONPATH=src python scripts/bench_chat.py --concurrency 8 --requests 24 --max-concurrency 2
PYTHONPATH=src python scripts/bench_chat.py --concurrency 8 --requests 24 --max-concurrency 8
# 打真实服务（真模型、真花钱）
PYTHONPATH=src python scripts/bench_chat.py --live --url http://127.0.0.1:8000 --token <TOKEN>
```

本地实测（模型桩延迟 300ms、24 个请求；数字随机器变化，重跑同一条命令对比）：

| `CHAT_MAX_CONCURRENCY` | 墙钟 | P50 | P95 | 吞吐 |
|---|---|---|---|---|
| 2 | 5.2s | 1.26s | 2.39s | 4.7 req/s |
| 8 | 2.7s | 0.48s | 1.75s | 9.0 req/s |

结论：墙钟 ≈ `ceil(请求数 / 闸门) × 模型延迟 + 固定开销`，固定开销每轮约 0.5~0.6s（会话/记忆/台账落库）。
真实模型下单轮 7~9s，瓶颈在 LLM；要提吞吐就加闸门 + 多副本 + 上游配额，而不是无脑加并发。

**写操作权限**：默认 `WRITE_TOOLS_ROLES=["admin"]`，部门账号调写工具会拿到「无权限执行写操作」（`error_kind=business`）。
演示补货/售后请用 `admin/hhw123`；要让部门账号也能建草稿，把该变量设成 `["admin","user"]` 并重启。
