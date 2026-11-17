"""查询改写用的业务同义词：中台治理下发 + 本地兜底（评审 3.4-10）。

为什么不能硬编码在 RAG 里：「断码 / 齐码 / 走货 / 尾货 / 翻单」这些词是**业务口径**，
改一次同义词表就要发一次版，说明这个知识放错地方了。现在：
- 中台 `GET /api/v1/metadata/synonyms` 是唯一真相（和指标口径一样由治理维护）；
- Agent 缓存一份（`SYNONYMS_TTL` 秒），拉不到就用内置兜底（离线/单测仍能跑）；
- `rewrite_query` 只用这张表，不再自己维护一份。
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger("sku-agent.rag.synonyms")

_FALLBACK: dict[str, list[str]] = {
    "断码": ["缺码", "尺码断货", "尺码不足"],
    "补货": ["订货", "采购", "补单"],
    "清仓": ["清库", "甩货", "折扣清仓"],
    "积压": ["滞销", "库存积压"],
    "退货率": ["退货", "售后率"],
    "大促": ["双11", "618", "年货节", "活动"],
    "换季": ["季末", "季节切换"],
    "缺货": ["断货", "无货"],
    "调拨": ["仓间调拨", "转仓"],
    "交期": ["到货时间", "供应商交期"],
    "齐码": ["全码", "码数齐全"],
    "生命周期": ["上新", "主推", "清仓", "长尾"],
}

_STATE: dict = {"table": dict(_FALLBACK), "source": "fallback", "at": 0.0, "error": ""}
_TTL_SECONDS = 1800.0


def reset() -> None:
    _STATE.update({"table": dict(_FALLBACK), "source": "fallback", "at": 0.0, "error": ""})


def synonyms_for(source_only: bool = False):
    table = _STATE["table"]
    return _STATE["source"] if source_only else table


async def refresh(force: bool = False) -> dict:
    """从平台拉同义词表（失败保留旧表 + 记录 error，不阻塞检索）。"""
    if not force and _STATE["at"] and (time.monotonic() - _STATE["at"]) < _TTL_SECONDS:
        return status()
    try:
        from app.tools.platform_tools import get_platform_client

        data = await get_platform_client().get("/api/v1/metadata/synonyms")
        table = (data or {}).get("synonyms") if isinstance(data, dict) else None
        if not table or (isinstance(data, dict) and data.get("error")):
            _STATE["error"] = str((data or {}).get("error") or "同义词为空")
            return status()
        cleaned = {str(k): [str(v) for v in (vals or [])][:12] for k, vals in table.items()}
        if cleaned:
            _STATE.update({"table": cleaned, "source": "platform",
                           "at": time.monotonic(), "error": ""})
            logger.info("同义词表已从平台同步：%d 组", len(cleaned))
    except Exception as exc:  # noqa: BLE001 —— 拉不到就用兜底表
        _STATE["error"] = f"{type(exc).__name__}: {exc}"
    return status()


def refresh_sync(force: bool = False) -> dict:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(refresh(force))
    return status()


def status() -> dict:
    return {"source": _STATE["source"], "count": len(_STATE["table"]),
            "error": _STATE["error"],
            "age_seconds": round(time.monotonic() - _STATE["at"], 1) if _STATE["at"] else None}
