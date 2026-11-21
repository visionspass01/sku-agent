"""worker 侧取参考图：`storage_key`（同后端对象）或 `url`（直接 HTTP 拉取）。

- 两种来源都限并发（`IMAGE_REFS_DOWNLOAD_CONCURRENCY`，默认 4）并受整体预算
  （`IMAGE_REFS_DOWNLOAD_BUDGET_SECONDS`，默认 20s）约束，绝不让单个请求永久挂起；
- 与父进程 `sources/refs.py` 的并发下载同一套节奏，避免"小文件串行"把任务拖长。
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.agents_image.worker.contract import WorkerRef
from app.agents_image.worker.storage import sha256_bytes, sniff_mime
from app.core.errors import ImageError, raise_image


async def _fetch_url(url: str, *, timeout: float, client: Any) -> bytes:
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content
    except Exception as exc:  # noqa: BLE001 —— 网络/超时/非 2xx
        raise_image("IMG_UPSTREAM_MCP_UNAVAILABLE",
                    f"下载参考图失败（{type(exc).__name__}）", retryable=True)


async def load_refs(refs: list[WorkerRef], *, storage: Any, timeout: float = 10.0,
                     concurrency: int = 4, budget_seconds: float = 20.0) -> list[dict]:
    """把引用解析成 `[{data, mime, sha256, source}]`（顺序与入参一致）。"""
    if not refs:
        return []
    # 调用方可能传 dict（HTTP/SCF 反序列化后未走模型），这里统一收敛成 WorkerRef
    refs = [item if isinstance(item, WorkerRef) else WorkerRef.model_validate(item)
            for item in refs]
    gate = asyncio.Semaphore(max(1, int(concurrency)))
    needs_http = any(item.kind == "url" for item in refs)
    client = None
    if needs_http:
        import httpx

        client = httpx.AsyncClient(timeout=timeout)

    async def _one(ref: WorkerRef) -> dict:
        async with gate:
            if ref.kind == "url":
                assert client is not None
                data = await _fetch_url(ref.value, timeout=timeout, client=client)
            elif ref.kind == "inline":
                import base64

                try:
                    data = base64.b64decode(str(ref.value or ""), validate=True)
                except Exception as exc:  # noqa: BLE001
                    raise_image("IMG_REFERENCE_INVALID", f"内联参考图 base64 解码失败：{exc}")
            else:
                # storage.get 是同步调用（SDK/内存），丢线程池避免阻塞事件循环
                data = await asyncio.to_thread(storage.get, ref.value)
        mime = ref.mime or sniff_mime(data) or "image/png"
        return {"data": data, "mime": mime, "sha256": sha256_bytes(data),
                "source": ref.kind}

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*[_one(item) for item in refs]),
            timeout=max(1.0, float(budget_seconds)))
    except (asyncio.TimeoutError, TimeoutError) as exc:
        raise ImageError("IMG_UPSTREAM_MCP_UNAVAILABLE",
                         f"参考图取数超时（{budget_seconds:g}s 预算内未完成 "
                         f"{len(refs)} 张）", retryable=True) from exc
    except ImageError:
        raise
    finally:
        if client is not None:
            await client.aclose()
    return list(results)


__all__ = ["load_refs"]
