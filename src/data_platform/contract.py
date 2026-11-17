"""中台对外数据契约的版本定义（单一真相）。

为什么需要它（评审 2.1-4）：
早先 `/api/v1/*` 的返回体是「裸 dict」，没有任何版本声明。多 Agent 同时接一个中台时，
字段一旦增删改，调用方只能靠运行时 KeyError 才发现——而那时错误已经穿过 Agent 落进用户答案里。
现在每个响应都有 `schema_version`，并且每个路由都用 pydantic `response_model` 声明形状
（`data_platform/api/schemas.py`），字段漂移会在 CI 立刻暴露。

版本策略（刻意简单，够用且不会把人绕晕）：
- **主版本**：不兼容变更（删字段/改语义/改单位）。调用方主版本不匹配 → 直接拒绝，不重试。
- **次版本**：只增字段、只增端点。调用方应向后兼容（多出来的字段忽略即可）。
- 中台通过响应头 `X-Platform-Contract` 主动声明自己的版本，Agent 侧
  `app/tools/platform_contract.py` 负责校验，不一致时给出可诊断的错误而不是静默降质。
"""

from __future__ import annotations

# 当前中台对外契约版本
CONTRACT_VERSION = "1.1"
# 中台仍接受的调用方契约版本（Agent 不带版本头时按 1.0 处理，保持既有部署零改动）
SUPPORTED_CLIENT_VERSIONS = ("1.0", "1.1")

CONTRACT_HEADER = "X-Platform-Contract"
DATA_VERSION_HEADER = "X-Data-Version"
CLIENT_CONTRACT_HEADER = "X-Agent-Contract"


def major_of(version: str) -> str:
    """取主版本号：`1.2.3` → `1`，`v1` → `1`，非法/空 → `""`。"""
    text = (version or "").strip().lstrip("vV")
    if not text:
        return ""
    return text.split(".", 1)[0]


def compatible(version: str) -> bool:
    """调用方版本能否与本中台互通：主版本相同即可（次版本差异只增不改）。"""
    return major_of(version) == major_of(CONTRACT_VERSION)


def contract_meta() -> dict:
    return {
        "contract_version": CONTRACT_VERSION,
        "supported_client_versions": list(SUPPORTED_CLIENT_VERSIONS),
        "policy": "主版本不兼容（删字段/改语义）；次版本只增字段，调用方需忽略未知字段",
    }
