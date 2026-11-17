# ---------- 构建阶段 ----------
FROM python:3.11-slim AS builder
WORKDIR /build
ARG PIP_INDEX_URL=https://pypi.org/simple
COPY requirements.docker.txt ./requirements.docker.txt
RUN pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" --prefix=/install -r requirements.docker.txt

# ---------- 运行阶段 ----------
FROM python:3.11-slim
WORKDIR /app
ENV PYTHONUTF8=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    PYTHONPATH=/app/src

COPY --from=builder /install /usr/local
COPY src ./src
COPY scripts ./scripts
COPY data ./data
COPY web ./web
COPY docs ./docs
# AGENTS.md 是 Agent 的 L2 工作区基线（运行时读取），不 COPY 会导致容器内基线为空
COPY AGENTS.md ./AGENTS.md

EXPOSE 8000 8001

# 默认：若数据文件不存在则先初始化（保留已有数据），再启动 Agent
# 平台服务在 docker-compose.deploy.yml 中通过 command 覆盖为 :8001 启动
CMD ["sh", "-c", "python scripts/ensure_init.py; exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
