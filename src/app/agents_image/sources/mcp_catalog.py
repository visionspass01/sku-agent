"""`PlatformCatalog` 的 mcp 实现：经 MCP 工具（`image_*`）取中台数据。

错误翻译（**不降级**，与 v6 决定一致）：
- `McpCallError` / 超时 / 连接失败 → `IMG_UPSTREAM_MCP_UNAVAILABLE`(503, retryable)
- 工具返回 `error_kind == "not_found"` → `IMG_PRODUCT_NOT_FOUND`(404)
- 其余业务错 / 字段不合 schema → `IMG_UPSTREAM_INVALID`(502)

下载商品图走工具模块的 `download_image`（同样失败即 503），保持"所有数据都经中台"。
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Awaitable, Callable

from app.agents_image.sources.product_source import (
    PlanPolicy,
    PlatformRules,
    Preset,
    ProductData,
    ProductImage,
    ProductSummary,
    SlotSpec,
)
from app.core.errors import ImageError

logger = logging.getLogger("sku-agent.image.catalog.mcp")


def _product_of(payload: dict, *, tool: str) -> ProductData:
    """把中台返回的商品对象校验成 `ProductData`（单商品/批量共用）。"""
    images = []
    for item in payload.get("images") or []:
        try:
            images.append(ProductImage.model_validate(item))
        except Exception as exc:  # noqa: BLE001 —— 中台字段不合 schema
            raise ImageError("IMG_UPSTREAM_INVALID",
                             f"商品图字段不合 schema：{exc}") from exc
    try:
        data = ProductData.model_validate({**payload, "images": []})
    except Exception as exc:  # noqa: BLE001
        raise ImageError("IMG_UPSTREAM_INVALID",
                         f"商品字段不合 schema（{tool}）：{exc}") from exc
    data.images = images
    return data

ToolCaller = Callable[[str, dict], Awaitable[dict]]


def _mcp_caller() -> ToolCaller:
    """默认调用方：走仓库通用 MCP 客户端（memory/stdio/direct 由 `MCP_TRANSPORT` 决定）。

    注意：**回写类工具禁用 TTL 缓存**，否则同样的 payload 在缓存窗口内不会真的写到中台。
    """

    async def call(tool: str, arguments: dict) -> dict:
        from app.mcp_client.client import get_mcp_hub

        hub = get_mcp_hub()
        return await hub.call("image", tool, arguments,
                              use_cache=(tool != "image_save_result"))

    return call


class McpCatalog:
    """中台取数（`IMAGE_PRODUCT_SOURCE=mcp` 时启用）。"""

    name = "mcp"

    def __init__(self, caller: ToolCaller | None = None, downloader=None) -> None:
        self._call = caller or _mcp_caller()
        self._download = downloader or _default_download

    # ------------------------------------------------------------ 内部
    async def _tool(self, name: str, arguments: dict) -> dict:
        try:
            payload = await self._call(name, arguments)
        except ImageError:
            raise
        except Exception as exc:  # noqa: BLE001 —— MCP 调用失败一律视为中台不可用
            raise ImageError("IMG_UPSTREAM_MCP_UNAVAILABLE",
                             f"MCP 工具 {name} 调用失败：{type(exc).__name__}: {exc}",
                             retryable=True) from exc
        if not isinstance(payload, dict):
            raise ImageError("IMG_UPSTREAM_INVALID", f"MCP 工具 {name} 返回非对象")
        if payload.get("error"):
            if payload.get("error_kind") == "not_found":
                raise ImageError("IMG_PRODUCT_NOT_FOUND", str(payload.get("error"))[:200])
            raise ImageError("IMG_UPSTREAM_INVALID",
                             f"MCP 工具 {name} 业务错误：{str(payload.get('error'))[:200]}")
        return payload

    # ------------------------------------------------------------ 商品
    async def list_products(self, *, keyword: str = "", limit: int = 20) -> list[ProductSummary]:
        payload = await self._tool("image_list_products",
                                   {"keyword": keyword, "limit": int(limit)})
        return [ProductSummary.model_validate(item) for item in payload.get("products") or []]

    async def get_products_context(self, product_ids: list[str]) -> dict[str, ProductData]:
        """批量取商品上下文（W2）：一次 MCP 调用拿多个商品，避免 N 次往返。"""
        ids = [str(item).strip() for item in product_ids if str(item).strip()]
        if not ids:
            return {}
        payload = await self._tool("image_get_products_context", {"product_ids": ids})
        out: dict[str, ProductData] = {}
        for item in payload.get("products") or []:
            data = _product_of(item, tool="image_get_products_context")
            out[data.product_id] = data
        missing = [product_id for product_id in ids if product_id not in out]
        if missing:
            raise ImageError("IMG_PRODUCT_NOT_FOUND", f"商品不存在：{missing[0]}")
        return out

    async def get_product(self, product_id: str) -> ProductData:
        payload = await self._tool("image_get_product", {"product_id": product_id})
        return _product_of(payload, tool="image_get_product")

    # ------------------------------------------------------------ 预设
    async def list_compositions(self, *, category: str = "",
                                platform: str = "") -> list[Preset]:
        payload = await self._tool("image_list_compositions",
                                   {"category": category, "platform": platform or "淘系"})
        return [Preset.model_validate({**item, "kind": "composition"})
                for item in payload.get("items") or []]

    async def list_scene_styles(self, *, category: str = "",
                                platform: str = "") -> list[Preset]:
        payload = await self._tool("image_list_scene_styles",
                                   {"category": category, "platform": platform or "淘系"})
        return [Preset.model_validate({**item, "kind": "scene_style"})
                for item in payload.get("items") or []]

    async def get_plan_policy(self, *, platform: str = "",
                              category: str = "") -> PlanPolicy:
        payload = await self._tool("image_get_plan_policy",
                                   {"platform": platform or "淘系", "category": category})
        raw = payload.get("plan_policy") or {}
        try:
            return PlanPolicy.model_validate({
                **raw,
                "slots": [SlotSpec.model_validate(item) for item in raw.get("slots") or []],
            })
        except Exception as exc:  # noqa: BLE001
            raise ImageError("IMG_UPSTREAM_INVALID", f"图位策略不合 schema：{exc}") from exc

    async def get_platform_rules(self, *, platform: str = "") -> PlatformRules:
        payload = await self._tool("image_get_platform_rules",
                                   {"platform": platform or "淘系"})
        try:
            return PlatformRules.model_validate(payload.get("platform_rules") or {})
        except Exception as exc:  # noqa: BLE001
            raise ImageError("IMG_UPSTREAM_INVALID", f"平台规则不合 schema：{exc}") from exc

    # ------------------------------------------------------------ 回写
    async def save_result(self, payload: dict) -> dict:
        # 工具签名是 `image_save_result(payload)`，MCP 侧的参数名就是 payload
        return await self._tool("image_save_result", {"payload": payload or {}})

    # ------------------------------------------------------------ 取图
    async def fetch_image_bytes(self, image: ProductImage) -> bytes:
        if image.bytes_b64:
            try:
                return base64.b64decode(image.bytes_b64, validate=True)
            except Exception as exc:  # noqa: BLE001
                raise ImageError("IMG_UPSTREAM_INVALID",
                                 f"商品图 base64 解码失败：{exc}") from exc
        if not image.url:
            raise ImageError("IMG_UPSTREAM_INVALID", f"商品图缺少地址：{image.image_id or '-'}")
        try:
            return await self._download(image.url)
        except ImageError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ImageError("IMG_UPSTREAM_MCP_UNAVAILABLE",
                             f"下载商品图失败：{type(exc).__name__}: {exc}",
                             retryable=True) from exc


async def _default_download(url: str) -> bytes:
    from app.agents_image.mcp import product_image_server as server

    try:
        return await server.download_image(url)
    except Exception as exc:  # noqa: BLE001
        raise ImageError("IMG_UPSTREAM_MCP_UNAVAILABLE",
                         f"下载商品图失败：{type(exc).__name__}: {exc}",
                         retryable=True) from exc


def build_catalog(**kwargs: Any) -> McpCatalog:  # pragma: no cover —— 演示/测试便捷入口
    return McpCatalog(**kwargs)


__all__ = ["McpCatalog", "build_catalog"]
