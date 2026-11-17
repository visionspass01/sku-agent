"""数据中台配置（环境变量可覆盖）。"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]  # src/data_platform/config.py -> 仓库根
DATA_DIR = BASE_DIR / "data" / "platform"
RAW_DIR = DATA_DIR / "raw"

DB_URL = os.getenv("PLATFORM_DB_URL", f"sqlite:///{(DATA_DIR / 'platform.db').as_posix()}")
# 数据源：默认**空**（中台用自己的 source_spec 自举）。
# 只有显式指定时才复用 Agent 演示库（本地便利）；容器里通常没有这个文件，
# 早先把它设成默认值导致 compose 里中台起不来 / mart 空表。
SOURCE_DB = Path(os.getenv("PLATFORM_SOURCE_DB", "").strip()) if os.getenv(
    "PLATFORM_SOURCE_DB", "").strip() else None

API_PORT = int(os.getenv("PLATFORM_API_PORT", "8001"))
INTERNAL_TOKEN = os.getenv("PLATFORM_INTERNAL_TOKEN", "demo-internal-token")

# ---------------------------------------------------------------- 多 Agent 治理（身份/作用域/配额）
# PLATFORM_API_KEYS 用 JSON 配多个调用方：key → scopes / departments / rate_limit。
# 例：{"sku-agent": {"key": "k-xxx", "scopes": ["read","write","approve"], "departments": null}}
# 未配置时回退到单一 INTERNAL_TOKEN（全权主体），保证既有部署/演示零改动。
API_KEYS_RAW = os.getenv("PLATFORM_API_KEYS", "").strip()
RATE_LIMIT_PER_MINUTE = int(os.getenv("PLATFORM_RATE_LIMIT_PER_MINUTE", "600"))
AUDIT_KEEP = int(os.getenv("PLATFORM_AUDIT_KEEP", "20000"))

CACHE_TTL_SECONDS = int(os.getenv("PLATFORM_CACHE_TTL", "300"))
CACHE_JITTER = float(os.getenv("PLATFORM_CACHE_JITTER", "0.2"))

# HITL 阈值：补货量或金额达到阈值需人工确认
REPLENISH_QTY_THRESHOLD = int(os.getenv("PLATFORM_REPLENISH_QTY_THRESHOLD", "200"))
REPLENISH_AMOUNT_THRESHOLD = float(os.getenv("PLATFORM_REPLENISH_AMOUNT_THRESHOLD", "50000"))

# 演示用部门映射（与 Agent 侧保持一致：男装部门=男装；女装部门=女装+童装）
DEPARTMENT_CATEGORIES = {
    "男装": ("男装",),
    "女装": ("女装", "童装"),
}


def department_of(category: str | None) -> str | None:
    for dept, cats in DEPARTMENT_CATEGORIES.items():
        if category in cats:
            return dept
    return None


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
