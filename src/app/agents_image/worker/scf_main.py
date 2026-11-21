"""腾讯云 SCF 入口（Python 运行时）：`main_handler(event, context)`。

两种触发方式都兼容：
- **API 网关**：`event` 是 `{"body": "<JSON 字符串>"}`（或已解析的 dict），响应 `{statusCode, headers, body}`；
- **函数间同步调用（父进程 Invoke RequestResponse）**：`event` 直接就是 `WorkerRequest` 的 dict，
  直接返回 `WorkerResult` 的 dict（父进程拿它做记账/登记资产）。

配置全部走 env（`IMAGE_*`），函数本身不连数据库、不读本地磁盘。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from app.agents_image.worker.contract import WorkerRequest
from app.agents_image.worker.runner import execute, result_to_json
from app.core.errors import ImageError, failure_payload

logger = logging.getLogger("sku-agent.image.worker.scf")


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    body = event.get("body") if isinstance(event, dict) else None
    if isinstance(body, str) and body.strip():
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ValueError(f"body 不是合法 JSON：{exc}") from exc
        return parsed if isinstance(parsed, dict) else {}
    if isinstance(body, dict):
        return body
    return event if isinstance(event, dict) else {}


def _trace_of(event: dict[str, Any]) -> str:
    headers = (event.get("headers") or {}) if isinstance(event, dict) else {}
    for key in ("x-trace-id", "X-Trace-Id"):
        if isinstance(headers, dict) and headers.get(key):
            return str(headers[key])
    return str(os.environ.get("IMAGE_TRACE_ID") or "")


def _api_gateway_response(payload: dict[str, Any], *, status: int = 200) -> dict[str, Any]:
    return {"statusCode": status,
            "headers": {"Content-Type": "application/json; charset=utf-8"},
            "body": json.dumps(payload, ensure_ascii=False)}


async def _handle(event: dict[str, Any]) -> dict[str, Any]:
    payload = _event_payload(event)
    trace = _trace_of(event)
    try:
        request = WorkerRequest.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 —— 契约/版本校验失败
        return {"ok": False, "error": {"code": "IMG_INPUT_INVALID",
                                       "detail": f"WorkerRequest 校验失败：{exc}",
                                       "retryable": False},
                "trace_id": trace}
    try:
        result = await execute(request)
    except ImageError as exc:
        return {**failure_payload(exc, trace), "ok": False}
    return result_to_json(result)


def main_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:  # noqa: ARG001
    """SCF 入口：同步返回结果（父进程用 RequestResponse 模式调用）。"""
    api_style = isinstance(event, dict) and ("body" in event or "requestContext" in event)
    try:
        result = asyncio.run(_handle(event or {}))
    except Exception as exc:  # noqa: BLE001 —— 任何未预期异常都要回结构化错误
        logger.exception("worker 未预期异常")
        result = {"ok": False, "error": {"code": "IMG_INTERNAL",
                                         "detail": f"{type(exc).__name__}: {exc}",
                                         "retryable": False},
                  "trace_id": _trace_of(event or {})}
    if api_style:
        status = 200 if result.get("ok") else 500
        return _api_gateway_response(result, status=status)
    return result


if __name__ == "__main__":  # pragma: no cover —— 本地手动跑一次（模拟 SCF 事件）
    import sys

    raw = sys.stdin.read()
    print(json.dumps(main_handler(json.loads(raw) if raw.strip() else {}),
                     ensure_ascii=False, indent=2))
