"""OpenTelemetry 钩子（可选开关，默认关闭）。

定位：自研的 `run_detail` 时间轴回答「这一轮为什么慢」，标准 trace 回答「跨服务的调用链」。
两者不冲突——JD 里常见的 Langfuse / Braintrust / OTel 就是后者。这里不绑定任何 SaaS，
只把 span 打到标准 OTLP endpoint，接不接由部署方决定：

- 未配置 `OTEL_EXPORTER_OTLP_ENDPOINT` → 全部 no-op（默认，零依赖）；
- 配了但没装 `opentelemetry-sdk` → 打一条 warning 后继续 no-op（不会影响主流程）；
- 配了且装了 SDK → 每次对话轮次 / 每次工具调用都会产生 span（属性复用 run_detail 字段）。

为什么不用 `opentelemetry-instrument` 自动注入：项目是单进程 + 自研指标，
手动埋点更可控，也避免把 opentelemetry 变成强依赖。
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from app.config import get_settings

logger = logging.getLogger("sku-agent.otel")

_tracer: Any = None
_ready = False
_warned = False


def reset_otel() -> None:
    """测试/热更新用：清掉缓存的 tracer（下次调用重新判断配置）。"""
    global _tracer, _ready, _warned
    _tracer = None
    _ready = False
    _warned = False


def _endpoint() -> str:
    return (getattr(get_settings(), "otel_exporter_otlp_endpoint", "") or "").strip()


def enabled() -> bool:
    return bool(_endpoint())


def _get_tracer():
    """懒初始化 tracer；配置了 endpoint 但缺依赖/初始化失败时返回 None（不抛错）。"""
    global _tracer, _ready, _warned
    if _ready:
        return _tracer
    _ready = True
    if not enabled():
        return None
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        if not _warned:
            logger.warning("OTEL_EXPORTER_OTLP_ENDPOINT 已配置，但未安装 opentelemetry-sdk，"
                           "span 导出已跳过（pip install opentelemetry-sdk "
                           "opentelemetry-exporter-otlp-proto-http）")
            _warned = True
        return None
    try:
        settings = get_settings()
        provider = TracerProvider(resource=Resource.create({
            "service.name": getattr(settings, "otel_service_name", "sku-agent"),
            "deployment.environment": "demo",
        }))
        provider.add_span_processor(BatchSpanProcessor(
            OTLPSpanExporter(endpoint=f"{_endpoint().rstrip('/')}/v1/traces")
        ))
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer("sku-agent")
        logger.info("OTel span 导出已启用：%s", _endpoint())
        return _tracer
    except Exception as exc:  # noqa: BLE001 —— 观测失败绝不影响业务
        if not _warned:
            logger.warning("OTel 初始化失败，span 导出已跳过：%s", exc)
            _warned = True
        return None


@contextlib.contextmanager
def span(name: str, **attributes: Any):
    """给一段逻辑加 span；未启用时退化为「空上下文」（拿到的对象是 None）。"""
    tracer = _get_tracer()
    if tracer is None:
        yield None
        return
    with tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is None or value == "":
                continue
            try:
                current.set_attribute(f"sku_agent.{key}", value)
            except Exception:  # noqa: BLE001 —— 属性类型不支持时忽略
                continue
        yield current


def otel_status() -> dict:
    """给 /api/health：是否开启、endpoint、SDK 是否可用。"""
    sdk_available = False
    try:
        import opentelemetry.sdk  # noqa: F401

        sdk_available = True
    except ImportError:
        sdk_available = False
    return {
        "enabled": enabled(),
        "endpoint": _endpoint() or "",
        "sdk_available": sdk_available,
        "service_name": getattr(get_settings(), "otel_service_name", "sku-agent"),
        "note": "自研 run_detail 时间轴 + 可选 OTLP span 导出（Langfuse/Jaeger/Tempo 都吃 OTLP）",
    }
