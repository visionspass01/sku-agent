"""独立生图 worker 的 HTTP 形态：`uvicorn app.agents_image.worker.http_app:app`。

- `POST /generate`：Bearer `IMAGE_WORKER_TOKEN`，body = `WorkerRequest`，返回 `WorkerResult`；
  业务失败（如第三方 5xx / 存储失败）返回 **200 + ok=false**，让调用方能拿到"已发生成本"；
  只有协议层错误（鉴权/参数非法/版本不匹配）才用 4xx。
- `GET /healthz`：进程存活 + 当前存储后端 + provider 名称。

父进程用 `IMAGE_WORKER_MODE=http` + `IMAGE_WORKER_URL` 调它；SCF 部署不需要这个文件。
"""

from __future__ import annotations

import hmac
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.agents_image.worker.contract import WorkerRequest
from app.agents_image.worker.runner import execute, result_to_json
from app.core.errors import ImageError, failure_payload

logger = logging.getLogger("sku-agent.image.worker.http")


def _expected_token() -> str:
    from app.agents_image.config import get_image_settings

    return str(getattr(get_image_settings(), "worker_token", "") or "").strip()


def _authorized(request: Request) -> bool:
    token = _expected_token()
    if not token:                     # 未配置令牌 = 只允许本机调试（不静默放行公网）
        return request.client is not None and request.client.host in {"127.0.0.1", "::1"}
    auth = request.headers.get("authorization", "")
    provided = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    return bool(provided) and hmac.compare_digest(provided, token)


app = FastAPI(title="image-agent worker", version="1.0.0")


@app.post("/generate")
async def generate(request: Request) -> JSONResponse:
    trace = request.headers.get("X-Trace-Id") or ""
    if not _authorized(request):
        return JSONResponse(status_code=401,
                            content={"detail": "worker 令牌无效或缺失", "trace_id": trace})
    try:
        raw = await request.json()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=400,
                            content={"detail": f"请求体不是合法 JSON：{exc}",
                                     "trace_id": trace})
    try:
        payload = WorkerRequest.model_validate(raw if isinstance(raw, dict) else {})
    except Exception as exc:  # noqa: BLE001 —— 版本/字段校验失败
        return JSONResponse(status_code=422,
                            content={"detail": f"WorkerRequest 校验失败：{exc}",
                                     "trace_id": trace})
    try:
        result = await execute(payload)
    except ImageError as exc:          # 协议/配置问题（如存储后端未配置）
        return JSONResponse(status_code=exc.status,
                            content=failure_payload(exc, trace))
    return JSONResponse(status_code=200, content=result_to_json(result))


@app.get("/healthz")
async def healthz() -> dict:
    from app.agents_image.config import get_image_settings

    settings = get_image_settings()
    return {"ok": True, "provider": settings.provider,
            "storage": settings.effective_worker_storage,
            "contract": "1"}
