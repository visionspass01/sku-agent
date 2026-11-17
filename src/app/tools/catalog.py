"""SKU 参考目录缓存（sku_id → style/category/department/color/size）。

为什么需要它（评审 耦合点1 的连带修复）：
Agent 侧做「部门作用域二次防御」时要判 `sku_id → category`（`app/security/scope.py`），
订单要判 `order_id → sku_id → category`。早先这两个判断都直接读 **Agent 自己的 `sku` / `mock_order`
表**——业务数据在 Agent 库里有一份私有副本，这既是"双份真相"，也是 MCP 层耦合的根源。

现在改成：启动时从 `GET /api/v1/metadata/catalog` 拉一份**元数据**目录（只含标识/分类，
不含库存销量事实）放进内存。中台的 `data_version` 变了就刷新；单点 miss 记入统计并通过
`scope.py` 的 fail-closed 语义拒绝（宁可说"无权限"，不猜）。

过渡期兜底：目录未加载（离线/纯单测环境）时，`sku_category()` 会回落到本地演示库的 `sku` 表。
这条路径在业务域全部上移（Wave 2）完成后会被删除 —— 静态扫描测试
`tests/test_no_business_db_in_agent.py` 只允许本文件出现该 import。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger("sku-agent.catalog")

_LOCK = threading.RLock()
_STATE: dict[str, Any] = {
    "loaded": False,
    "at": 0.0,
    "data_version": None,
    "skus": {},        # sku_id -> {style_id, category, department, color, size, ...}
    "by_style": {},    # style_id -> [sku dict, ...]
    "error": "",
    "misses": 0,
    "refreshes": 0,
}


def reset() -> None:
    """测试用：清空缓存。"""
    with _LOCK:
        _STATE.update({"loaded": False, "at": 0.0, "data_version": None, "skus": {},
                       "by_style": {}, "error": "", "misses": 0, "refreshes": 0})


def is_loaded() -> bool:
    return bool(_STATE["loaded"])


def status() -> dict:
    with _LOCK:
        return {
            "loaded": bool(_STATE["loaded"]),
            "skus": len(_STATE["skus"]),
            "styles": len(_STATE["by_style"]),
            "data_version": _STATE["data_version"],
            "age_seconds": round(time.monotonic() - _STATE["at"], 1) if _STATE["at"] else None,
            "misses": _STATE["misses"],
            "refreshes": _STATE["refreshes"],
            "error": _STATE["error"],
            "source": "platform" if _STATE["loaded"] else "local-fallback",
        }


def _index(items: list[dict]) -> tuple[dict, dict]:
    skus: dict[str, dict] = {}
    by_style: dict[str, list[dict]] = {}
    for item in items:
        sku_id = str(item.get("sku_id") or "")
        if not sku_id:
            continue
        skus[sku_id] = item
        by_style.setdefault(str(item.get("style_id") or ""), []).append(item)
    return skus, by_style


async def sync(force: bool = False, page_limit: int = 2000, max_pages: int = 20) -> dict:
    """从平台拉全量目录（分页）。失败只记 error，不抛错（不阻塞启动）。"""
    from app.tools.platform_tools import get_platform_client

    client = get_platform_client()
    items: list[dict] = []
    data_version = None
    offset = 0
    for _ in range(max_pages):
        page = await client.get("/api/v1/metadata/catalog",
                                params={"offset": offset, "limit": page_limit})
        if not isinstance(page, dict) or page.get("error"):
            with _LOCK:
                _STATE["error"] = str((page or {}).get("error") or "目录接口返回异常")
            logger.warning("SKU 目录同步失败：%s", _STATE["error"])
            return status()
        chunk = page.get("items") or []
        items.extend(chunk)
        data_version = page.get("data_version", data_version)
        next_offset = page.get("next_offset")
        if not next_offset or not chunk:
            break
        offset = int(next_offset)
    if not items and not force:
        with _LOCK:
            _STATE["error"] = "目录为空（中台 mart 可能还没初始化）"
        return status()
    skus, by_style = _index(items)
    with _LOCK:
        _STATE.update({
            "loaded": bool(skus), "at": time.monotonic(), "data_version": data_version,
            "skus": skus, "by_style": by_style, "error": "", "refreshes": _STATE["refreshes"] + 1,
        })
    logger.info("SKU 目录已从平台同步：%d 个 SKU / %d 款式 / data_version=%s",
                len(skus), len(by_style), data_version)
    return status()


def _local_fallback(sku_id: str) -> dict | None:
    """过渡期兜底：目录没加载时读本地演示库（Wave 2 完成后删除）。"""
    try:
        from app.db import repos
        from app.db.engine import session_scope

        with session_scope() as session:
            sku = repos.get_sku(session, sku_id)
            if sku is None:
                return None
            return {"sku_id": sku.sku_id, "style_id": sku.style_id, "category": sku.category,
                    "color": sku.color, "size": sku.size, "season": sku.season,
                    "warehouse_id": sku.warehouse_id}
    except Exception as exc:  # noqa: BLE001 —— 兜底路径失败不该影响鉴权判定
        logger.debug("目录兜底查询失败：%s", exc)
        return None


def sku(sku_id: str) -> dict | None:
    """取一个 SKU 的元数据；目录未加载时回落本地演示库。"""
    if not sku_id:
        return None
    with _LOCK:
        loaded = bool(_STATE["loaded"])
        found = _STATE["skus"].get(sku_id) if loaded else None
        if loaded and found is None:
            _STATE["misses"] += 1
    if found is not None:
        return found
    return _local_fallback(sku_id)


def sku_category(sku_id: str) -> str | None:
    found = sku(sku_id)
    return (found or {}).get("category") or None


def style_of(sku_id: str) -> str | None:
    found = sku(sku_id)
    return (found or {}).get("style_id") or None


def style_category(style_id: str) -> str | None:
    """款 → 品类（款级工具的部门作用域判定用）。"""
    if not style_id:
        return None
    with _LOCK:
        rows = list(_STATE["by_style"].get(style_id) or []) if _STATE["loaded"] else []
    if rows:
        return rows[0].get("category") or None
    try:
        from app.db import repos
        from app.db.engine import session_scope

        with session_scope() as session:
            siblings = repos.get_style_skus(session, style_id)
            return siblings[0].category if siblings else None
    except Exception:  # noqa: BLE001
        return None


def style_skus(style_id: str) -> list[dict]:
    """同款全尺码（断码/尺码结构分析用）；目录未加载时回落本地库。"""
    with _LOCK:
        loaded = bool(_STATE["loaded"])
        rows = list(_STATE["by_style"].get(style_id) or []) if loaded else []
    if rows or loaded:
        return rows
    try:
        from app.db import repos
        from app.db.engine import session_scope

        with session_scope() as session:
            return [
                {"sku_id": s.sku_id, "style_id": s.style_id, "category": s.category,
                 "color": s.color, "size": s.size, "stock": s.stock, "price": s.price}
                for s in repos.get_style_skus(session, style_id)
            ]
    except Exception:  # noqa: BLE001
        return []


def needs_refresh(data_version: int | None, ttl_seconds: float = 300.0) -> bool:
    """目录是否该刷新：数据版本变了，或缓存超过 TTL。"""
    with _LOCK:
        if not _STATE["loaded"]:
            return True
        if data_version is not None and _STATE["data_version"] not in (None, data_version):
            return True
        return (time.monotonic() - float(_STATE["at"] or 0.0)) > ttl_seconds
