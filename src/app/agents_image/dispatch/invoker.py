"""把"调用独立 worker"抽象成可切换的 Invoker：`inproc` / `http` / `scf`。

- `inproc`（默认）：直接 `await worker.runner.execute(...)`，与历史行为一致（离线可测）；
- `http`：`POST {IMAGE_WORKER_URL}/generate`（Bearer `IMAGE_WORKER_TOKEN`），独立服务形态；
- `scf`：腾讯云 SCF 函数间**同步**调用（`RequestResponse`），SDK 惰性导入。

约定：`invoke()` 返回 `WorkerResult`；基础设施性失败（连不上/超时/契约不匹配）抛
`ImageError`（可重试），**业务性失败**由 `WorkerResult.ok=false + error` 表达——
两者都会让父进程照真实值记账（发起量/张数/成本）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Protocol, runtime_checkable

import httpx

from app.agents_image.worker.contract import WorkerRequest, WorkerResult
from app.core.errors import raise_image

logger = logging.getLogger("sku-agent.image.dispatch.invoker")


@runtime_checkable
class Invoker(Protocol):
    name: str

    async def invoke(self, request: WorkerRequest) -> WorkerResult: ...

    async def health(self) -> dict: ...


def _result_of(payload: Any) -> WorkerResult:
    """把 HTTP/SCF 的返回统一解析成 `WorkerResult`（dict 或 JSON 字符串都接受）。"""
    raw = payload
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise_image("IMG_PROVIDER_UNAVAILABLE",
                        f"worker 返回不是合法 JSON：{exc}", retryable=True)
    if not isinstance(raw, dict):
        raise_image("IMG_PROVIDER_UNAVAILABLE",
                    f"worker 返回类型不支持：{type(raw).__name__}", retryable=True)
    if "error" in raw and raw.get("ok") is None:      # HTTP 层错误体
        detail = str(raw.get("detail") or raw.get("error") or "worker 调用失败")
        raise_image("IMG_PROVIDER_UNAVAILABLE", detail, retryable=True)
    return WorkerResult.model_validate(raw)


class InProcessInvoker:
    """进程内直调（默认；全部离线测试与本地演示走这条）。"""

    name = "inproc"

    async def invoke(self, request: WorkerRequest) -> WorkerResult:
        from app.agents_image.worker.runner import execute

        return await execute(request)

    async def health(self) -> dict:
        return {"ok": True, "invoker": self.name}


class HttpInvoker:
    """独立 HTTP 服务形态：`uvicorn app.agents_image.worker.http_app:app`。"""

    name = "http"

    def __init__(self, *, url: str = "", token: str = "", timeout: float = 300.0) -> None:
        from app.agents_image.config import get_image_settings

        settings = get_image_settings()
        self.url = (url or settings.worker_url).rstrip("/")
        self.token = token or settings.worker_token
        self.timeout = float(timeout or settings.worker_timeout_seconds)
        if not self.url:
            raise_image("IMG_PROVIDER_UNAVAILABLE",
                        "IMAGE_WORKER_MODE=http 需要配置 IMAGE_WORKER_URL")

    async def invoke(self, request: WorkerRequest) -> WorkerResult:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if request.trace_id:
            headers["X-Trace-Id"] = request.trace_id
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(f"{self.url}/generate",
                                         content=request.model_dump_json(),
                                         headers=headers)
        except Exception as exc:  # noqa: BLE001 —— 连接/超时
            raise_image("IMG_PROVIDER_UNAVAILABLE",
                        f"worker 服务不可用（{type(exc).__name__}）", retryable=True, )
        if resp.status_code == 401:
            raise_image("IMG_PROVIDER_UNAVAILABLE", "worker 令牌无效（IMAGE_WORKER_TOKEN）")
        if resp.status_code == 422:
            raise_image("IMG_INPUT_INVALID", f"worker 契约校验失败：{resp.text[:200]}")
        if resp.status_code >= 500:
            raise_image("IMG_PROVIDER_UNAVAILABLE",
                        f"worker 服务错误 HTTP {resp.status_code}", retryable=True)
        if resp.status_code >= 400:
            raise_image("IMG_PROVIDER_UNAVAILABLE",
                        f"worker 拒绝请求 HTTP {resp.status_code}：{resp.text[:200]}")
        try:
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise_image("IMG_PROVIDER_UNAVAILABLE", f"worker 返回解析失败：{exc}",
                        retryable=True)
        return _result_of(payload)

    async def health(self) -> dict:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.url}/healthz")
            return {"ok": resp.status_code == 200, "invoker": self.name,
                    "status": resp.status_code}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "invoker": self.name, "error": type(exc).__name__}


class ScfInvoker:
    """腾讯云 SCF 同步调用（`InvocationType=RequestResponse`）。

    函数侧用 `app.agents_image.worker.scf_main.main_handler`；父进程把 `WorkerRequest`
    作为事件 payload 传过去，函数直接返回 `WorkerResult`。
    """

    name = "scf"

    def __init__(self, *, function_name: str = "", region: str = "", namespace: str = "",
                 timeout: float = 0.0) -> None:
        from app.agents_image.config import get_image_settings

        settings = get_image_settings()
        self.function_name = function_name or settings.scf_function_name
        self.region = region or settings.scf_region
        self.namespace = namespace or settings.scf_namespace or "default"
        self.timeout = float(timeout or settings.scf_invoke_timeout_seconds)
        if not (self.function_name and self.region):
            raise_image("IMG_PROVIDER_UNAVAILABLE",
                        "IMAGE_WORKER_MODE=scf 需要配置 IMAGE_SCF_FUNCTION_NAME 与 IMAGE_SCF_REGION")

    def _client(self):  # noqa: ANN202 —— 第三方客户端
        try:
            from tencentcloud.common import credential  # type: ignore[import-not-found]
            from tencentcloud.scf.v20180416 import (  # type: ignore[import-not-found]
                models,
                scf_client,
            )
        except Exception as exc:  # noqa: BLE001 —— 没装 SDK
            raise_image("IMG_PROVIDER_UNAVAILABLE",
                        f"未安装 SCF SDK（pip install tencentcloud-sdk-python）：{exc}")
        import os

        secret_id = os.environ.get("TENCENTCLOUD_SECRET_ID", "")
        secret_key = os.environ.get("TENCENTCLOUD_SECRET_KEY", "")
        if not (secret_id and secret_key):
            raise_image("IMG_PROVIDER_UNAVAILABLE",
                        "SCF 调用需要 TENCENTCLOUD_SECRET_ID / TENCENTCLOUD_SECRET_KEY")
        client = scf_client.ScfClient(credential.Credential(secret_id, secret_key),
                                      self.region)
        return client, models

    async def invoke(self, request: WorkerRequest) -> WorkerResult:
        client, models = self._client()
        params = models.InvokeRequest()
        params.FunctionName = self.function_name
        params.Namespace = self.namespace
        params.InvocationType = "RequestResponse"
        params.ClientContext = json.dumps(request.model_dump(mode="json"), ensure_ascii=False)
        try:
            resp = await asyncio.to_thread(client.Invoke, params)
        except Exception as exc:  # noqa: BLE001 —— 网络/鉴权/函数不存在
            raise_image("IMG_PROVIDER_UNAVAILABLE",
                        f"SCF 调用失败（{type(exc).__name__}）", retryable=True)
        ret = getattr(getattr(resp, "Result", None), "Ret", None)
        if ret in (None, ""):
            raise_image("IMG_PROVIDER_UNAVAILABLE", "SCF 返回为空", retryable=True)
        return _result_of(ret)

    async def health(self) -> dict:
        return {"ok": True, "invoker": self.name, "function": self.function_name,
                "region": self.region}


_MODES = {"inproc": InProcessInvoker, "http": HttpInvoker, "scf": ScfInvoker}
_INSTANCES: dict[str, Invoker] = {}


def get_invoker(mode: str | None = None) -> Invoker:
    """按 `IMAGE_WORKER_MODE` 取 invoker（进程内单例）。"""
    from app.agents_image.config import get_image_settings

    key = (mode or get_image_settings().worker_mode or "inproc").strip().lower()
    factory = _MODES.get(key)
    if factory is None:
        raise_image("IMG_PROVIDER_UNAVAILABLE",
                    f"不支持的 IMAGE_WORKER_MODE={key}（可选：{'、'.join(sorted(_MODES))}）")
    if key not in _INSTANCES:
        _INSTANCES[key] = factory()
    return _INSTANCES[key]


def reset_invokers() -> None:
    _INSTANCES.clear()


__all__ = ["Invoker", "InProcessInvoker", "HttpInvoker", "ScfInvoker",
           "get_invoker", "reset_invokers"]
