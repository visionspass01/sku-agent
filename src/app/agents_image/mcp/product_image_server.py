"""图片中台 MCP Server（FastMCP）：7 个工具，把 Agent 的取数/回写映射到中台 REST。

为什么单独一份：`app/mcp_servers/_platform.py` 是 SKU 侧适配层（依赖 `app.config` 的 SKU 设置），
按解耦约定 `agents_image` 不能依赖它；这里自带一个更小的 httpx 适配（同一套超时/重试语义）。

失败语义（v6，**不降级**）：
- 5xx / 连接失败 / 超时 → 抛 `ImagePlatformError`（MCP 侧变成工具错误）；
- 4xx → 返回 `{"error": ..., "error_kind": ...}`，由调用方（`McpCatalog`）翻译成
  `IMG_PRODUCT_NOT_FOUND` / `IMG_UPSTREAM_INVALID` 等业务错误码。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import httpx

from app.agents_image.config import get_image_settings

logger = logging.getLogger("sku-agent.image.mcp")

IMAGE_TOOL_NAMES = (
    "image_list_products",
    "image_get_product",
    "image_get_products_context",   # W2：批量商品上下文（3 秒窗口合并后的单次调用）
    "image_list_compositions",
    "image_list_scene_styles",
    "image_get_plan_policy",
    "image_get_platform_rules",
    "image_save_result",
)


class ImagePlatformError(RuntimeError):
    """中台链路故障（5xx/连接失败/超时）：调用方应翻译成 503 `IMG_UPSTREAM_MCP_UNAVAILABLE`。"""


_transport: httpx.AsyncBaseTransport | None = None
_client: httpx.AsyncClient | None = None
_client_loop: Any = None


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """测试注入（`httpx.MockTransport` / `ASGITransport`）；置 None 恢复真实网络。"""
    global _transport, _client, _client_loop
    _transport = transport
    _client = None
    _client_loop = None


def reset_transport() -> None:
    set_transport(None)


def _client_for_loop() -> httpx.AsyncClient:
    global _client, _client_loop
    try:
        loop: Any = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover —— 同步上下文兜底
        loop = None
    if _client is None or _client.is_closed or (_client_loop is not loop):
        settings = get_image_settings()
        _client = httpx.AsyncClient(
            transport=_transport,
            timeout=float(settings.mcp_timeout_seconds),
            trust_env=False,
        )
        _client_loop = loop
    return _client


async def aclose() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _headers() -> dict[str, str]:
    settings = get_image_settings()
    trace_id = ""
    try:
        from app.observability import trace as trace_mod

        trace_id = trace_mod.current_trace_id()
    except Exception:  # noqa: BLE001 —— 观测失败不影响取数
        trace_id = ""
    request_id = f"{trace_id}:mcp-image:{uuid.uuid4().hex[:6]}" if trace_id \
        else f"mcp-image:{uuid.uuid4().hex[:6]}"
    headers = {
        "X-Internal-Token": settings.platform_auth_token,
        "X-Agent-Id": "mcp-image",
        "X-Request-Id": request_id,
        "X-Trace-Id": trace_id,
    }
    return headers


async def _request(method: str, path: str, *, params: dict | None = None,
                   json_body: dict | None = None) -> dict:
    """调一个中台端点：5xx/网络失败重试 `IMAGE_MCP_RETRIES` 次后抛 `ImagePlatformError`。"""
    settings = get_image_settings()
    url = settings.platform_url.rstrip("/") + path
    attempts = max(0, int(settings.mcp_retries)) + 1
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            response = await _client_for_loop().request(
                method, url, params=params, json=json_body, headers=_headers())
        except Exception as exc:  # noqa: BLE001 —— 连接/超时
            last_error = exc
            if attempt + 1 < attempts:
                await asyncio.sleep(0.05 * (attempt + 1))
                continue
            raise ImagePlatformError(
                f"数据中台不可用（{type(exc).__name__}）：{exc}") from exc
        if response.status_code >= 500:
            last_error = ImagePlatformError(f"中台接口错误 HTTP {response.status_code}")
            if attempt + 1 < attempts:
                await asyncio.sleep(0.05 * (attempt + 1))
                continue
            raise last_error
        if response.status_code >= 400:
            return {"error": _detail(response), "error_kind": _kind_of(response.status_code),
                    "status_code": response.status_code}
        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise ImagePlatformError(f"中台响应不是合法 JSON：{exc}") from exc
        return payload if isinstance(payload, dict) else {"items": payload}
    raise ImagePlatformError(str(last_error or "中台调用失败"))  # pragma: no cover


def _kind_of(status: int) -> str:
    return {400: "invalid", 401: "auth", 403: "permission", 404: "not_found"}.get(
        status, "business")


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
        if isinstance(body, dict):
            return str(body.get("detail") or body.get("error") or "")[:300]
    except Exception:  # noqa: BLE001
        pass
    return (response.text or "")[:300]


def _is_error(payload: Any) -> bool:
    return isinstance(payload, dict) and bool(payload.get("error"))


# ---------------------------------------------------------------- 工具实现（纯函数，便于直测）
async def image_list_products(keyword: str = "", limit: int = 20) -> dict:
    """商品清单（供前端多选）：product_id / title / category / main_image_id。"""
    limit = max(1, min(int(limit or 20), 100))
    data = await _request("GET", "/api/v1/image/products",
                          params={"keyword": keyword or "", "limit": limit})
    if _is_error(data):
        return data
    return {"products": data.get("products") or [], "count": int(data.get("count") or 0)}


async def image_get_product(product_id: str) -> dict:
    """商品身份锁定字段 + 商品参考图（images[]：image_id / url / mime / is_main）。"""
    product_id = str(product_id or "").strip()
    if not product_id:
        return {"error": "product_id 不能为空", "error_kind": "invalid", "status_code": 400}
    data = await _request("GET", f"/api/v1/image/products/{product_id}/context")
    if _is_error(data):
        return data
    return {
        "product_id": data.get("product_id", product_id),
        "title": data.get("title", ""),
        "category": data.get("category", ""),
        "attrs": data.get("attrs") or {},
        "images": data.get("images") or [],
        "sku_ids": data.get("sku_ids") or [],
    }


async def image_get_products_context(product_ids: list[str]) -> dict:
    """批量取商品上下文（W2）：Agent 把 3 秒窗口内的商品 id 合并成一次调用。"""
    ids = [str(item).strip() for item in (product_ids or []) if str(item).strip()]
    if not ids:
        return {"products": [], "missing": [], "error": ""}
    payload = await _request("GET", "/api/v1/image/products/context",
                             params={"ids": ",".join(ids)})
    if payload is None:
        return {"products": [], "missing": ids, "error": "中台批量上下文不可用"}
    products = list(payload.get("products") or [])
    missing = list(payload.get("missing") or [])
    if not products and missing:
        return {"products": [], "missing": missing, "error": "商品不存在",
                "error_kind": "not_found"}
    return {"products": products, "missing": missing, "error": ""}


async def image_list_compositions(category: str = "", platform: str = "淘系") -> dict:
    """构图候选（composition_id / label / desc / prompt_hint）。"""
    data = await _request("GET", "/api/v1/image/presets/compositions",
                          params={"platform": platform, "category": category})
    if _is_error(data):
        return data
    return {"items": data.get("items") or [], "count": int(data.get("count") or 0)}


async def image_list_scene_styles(category: str = "", platform: str = "淘系") -> dict:
    """场景画面风格候选（scene_style_id / label / desc / prompt_hint）。"""
    data = await _request("GET", "/api/v1/image/presets/scene-styles",
                          params={"platform": platform, "category": category})
    if _is_error(data):
        return data
    return {"items": data.get("items") or [], "count": int(data.get("count") or 0)}


async def image_get_plan_policy(platform: str = "淘系", category: str = "") -> dict:
    """9 图位框架 + 默认张数 + 默认构图/场景风格。"""
    data = await _request("GET", "/api/v1/image/plan-policy",
                          params={"platform": platform, "category": category})
    if _is_error(data):
        return data
    return {"plan_policy": data}


async def image_get_platform_rules(platform: str = "淘系") -> dict:
    """平台图片规范卡（尺寸/背景/文字/文件大小/禁用词）。"""
    data = await _request("GET", f"/api/v1/image/platform-rules/{platform}")
    if _is_error(data):
        return data
    return {"platform_rules": data}


async def image_save_result(payload: dict) -> dict:
    """回写结果元数据（不含图片二进制）→ {saved, deduped}。"""
    data = await _request("POST", "/api/v1/image/results", json_body=payload or {})
    if _is_error(data):
        return data
    return {"saved": int(data.get("saved") or 0), "deduped": int(data.get("deduped") or 0),
            "task_id": data.get("task_id", ""), "request_id": data.get("request_id", "")}


async def download_image(url: str) -> bytes:
    """下载商品图（`url` 可能是中台相对路径）；失败抛 `ImagePlatformError`。"""
    settings = get_image_settings()
    target = url if url.startswith("http") else settings.platform_url.rstrip("/") + url
    try:
        response = await _client_for_loop().get(target, headers=_headers())
        response.raise_for_status()
        return response.content
    except Exception as exc:  # noqa: BLE001 —— 网络/超时/非 2xx
        raise ImagePlatformError(f"下载商品图失败：{type(exc).__name__}: {exc}") from exc


IMAGE_TOOLS = {
    "image_list_products": image_list_products,
    "image_get_product": image_get_product,
    "image_get_products_context": image_get_products_context,
    "image_list_compositions": image_list_compositions,
    "image_list_scene_styles": image_list_scene_styles,
    "image_get_plan_policy": image_get_plan_policy,
    "image_get_platform_rules": image_get_platform_rules,
    "image_save_result": image_save_result,
}


def _build_server():
    """FastMCP 注册（memory/stdio transport 用；direct transport 直接用 IMAGE_TOOLS）。"""
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("image-mcp")
    server.tool()(image_list_products)
    server.tool()(image_get_product)
    server.tool()(image_get_products_context)
    server.tool()(image_list_compositions)
    server.tool()(image_list_scene_styles)
    server.tool()(image_get_plan_policy)
    server.tool()(image_get_platform_rules)
    server.tool()(image_save_result)
    return server


mcp = _build_server()


if __name__ == "__main__":  # pragma: no cover —— 手动起 MCP 服务时用
    mcp.run(transport="stdio")
