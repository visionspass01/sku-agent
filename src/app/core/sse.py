"""SSE 事件封装：与 SKU 侧的事件命名保持一致（stage / delta / done / fatal）。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable
from typing import Any

from fastapi.responses import StreamingResponse


def sse_event(event: str, data: dict[str, Any] | str) -> str:
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def sse_comment(text: str = "") -> str:
    """SSE 注释行（`: keepalive`）：用于心跳，浏览器会忽略。"""
    return f": {text}\n\n"


async def sse_from_events(events: Iterable[str]) -> AsyncIterator[str]:
    for chunk in events:
        yield chunk


def sse_response(stream: AsyncIterator[str]) -> StreamingResponse:
    """包装成 SSE 响应：关缓存 + 关 nginx 缓冲（否则代理会攒满一屏才下发）。"""
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
