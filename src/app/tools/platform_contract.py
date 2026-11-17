"""中台契约校验（Agent 侧）。

为什么单独一个模块（评审 2.1-4）：
中台返回体是"裸 JSON"，字段一旦改名/删字段，Agent 只能靠 `KeyError` 或 `None` 在**用户答案里**
暴露问题。现在中台每个响应都带 `schema_version` 与 `X-Platform-Contract` 头
（见 `data_platform/contract.py`），Agent 在 `PlatformClient` 收口处统一校验：

- **主版本不同** → 立刻返回 `error_kind="contract"` 的业务错误，不重试、不进缓存、
  不计熔断（中台是健康的，是我们的契约对不上），并给出「联系中台升级」的下一步提示；
- **缺少版本号** → 视为 legacy 1.0（兼容既有部署与测试桩），只计数不报错；
  `PLATFORM_CONTRACT_STRICT=true` 时才当成错误——生产建议打开，让"漏版本号"在灰度就暴露。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("sku-agent.platform.contract")

# Agent 自身实现的契约版本（与中台 data_platform/contract.py 的主版本必须一致）
AGENT_CONTRACT_VERSION = "1.1"
# 没有版本号时的兜底版本：等价于"中台还没升级"，按 1.0 语义解析
LEGACY_VERSION = "1.0"

_STATS = {"seen": 0, "legacy": 0, "mismatch": 0, "last_remote": "", "last_error": ""}


def major_of(version: str) -> str:
    text = (version or "").strip().lstrip("vV")
    return text.split(".", 1)[0] if text else ""


def compatible(remote: str) -> bool:
    """主版本一致即兼容（次版本只增字段，旧解析器忽略多余字段即可）。"""
    return major_of(remote) == major_of(AGENT_CONTRACT_VERSION)


def reset_stats() -> None:
    _STATS.update({"seen": 0, "legacy": 0, "mismatch": 0, "last_remote": "", "last_error": ""})


def stats() -> dict:
    return {**_STATS, "local": AGENT_CONTRACT_VERSION}


def check(payload, header_version: str = "", *, strict: bool = False) -> dict | None:
    """校验一个中台响应。返回业务错误 dict（`error_kind="contract"`）或 None（通过）。"""
    _STATS["seen"] += 1
    remote = (header_version or "").strip()
    if not remote and isinstance(payload, dict):
        remote = str(payload.get("schema_version") or "").strip()
    if not remote:
        _STATS["legacy"] += 1
        _STATS["last_remote"] = LEGACY_VERSION
        if strict:
            _STATS["mismatch"] += 1
            return _contract_error(
                f"响应缺少契约版本号（Agent 要求 >= {AGENT_CONTRACT_VERSION}）"
            )
        return None
    _STATS["last_remote"] = remote
    if compatible(remote):
        return None
    _STATS["mismatch"] += 1
    return _contract_error(
        f"中台契约版本 {remote} 与 Agent 支持的 {AGENT_CONTRACT_VERSION} 不兼容"
    )


def _contract_error(message: str) -> dict:
    _STATS["last_error"] = message
    logger.warning("中台契约不兼容：%s", message)
    return {
        "error": message,
        "error_kind": "contract",
        # 给模型看的下一步：契约问题换参数/换工具都没用，必须人工介入
        "next_step": ("这是接口契约不匹配（不是数据问题）：不要重试任何取数工具，"
                      "直接告知用户「数据中台接口版本不一致，请联系管理员升级」"),
    }
