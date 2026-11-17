# 常用命令（Linux/macOS/Git Bash；Windows 可用同样目标名配合 make，或参考 README 的 PowerShell 版本）
PY ?= python
PYTHONPATH_SRC := src

.PHONY: help install seed platform-seed dev platform test smoke eval demo stack clean

help:  ## 显示可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:  ## 安装运行 + 开发依赖
	$(PY) -m pip install -r requirements.txt -r requirements-dev.txt

seed:  ## 生成演示数据（300 SKU × 30 天 + 知识库索引 + 订单样例）
	PYTHONPATH=$(PYTHONPATH_SRC) $(PY) scripts/init_db.py

platform-seed:  ## 初始化数据中台（模拟源 → 入湖 → dbt 转换）
	PYTHONPATH=$(PYTHONPATH_SRC) $(PY) scripts/platform_bootstrap.py

dev:  ## 启动 Agent（:8000，API + 看板）
	PYTHONPATH=$(PYTHONPATH_SRC) $(PY) -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

platform:  ## 启动数据中台（:8001）
	PYTHONPATH=$(PYTHONPATH_SRC) $(PY) -m uvicorn data_platform.api.main:app --host 127.0.0.1 --port 8001 --reload

test:  ## 跑全部离线测试（1061 passed / 1062 收集）
	PYTHONPATH=$(PYTHONPATH_SRC) $(PY) -m pytest

smoke:  ## 前端 DOM 冒烟（无需浏览器）
	node scripts/ui_smoke.js

eval:  ## RAG 检索评测（Recall@K / MRR / nDCG@5）
	PYTHONPATH=$(PYTHONPATH_SRC) $(PY) scripts/eval_rag.py

demo:  ## 单 SKU 全链路演示（真实 LLM）
	PYTHONPATH=$(PYTHONPATH_SRC) $(PY) scripts/demo_single.py

stack:  ## 起 Docker 全套（MySQL + Redis + Qdrant + Agent + 中台 + nginx）
	docker compose -f docker/docker-compose.deploy.yml up -d --build

clean:  ## 清理本地生成物（可复现，会删演示库和生成的分析档案）
	rm -rf data/sku_demo.db data/sku_demo.db-* data/platform data/analyses data/artifacts data/qdrant_local
