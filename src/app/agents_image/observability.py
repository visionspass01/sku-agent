"""`/api/image/metrics`：把生图链路的计数器与阶段耗时导出成 **Prometheus text format**。

第五轮 P1-7 / 简历 4.5：
- 生图 Agent 过去只有"进程内计数 + health JSON"，没有可被 Prometheus 抓取的端点；
- 这里实现零依赖的 text format 渲染器（`# HELP` / `# TYPE` + 样本行），格式与
  `prometheus_client.generate_latest()` 兼容；若环境里装了 `prometheus_client`，
  优先用官方实现（指标名与标签保持一致）。
- 指标面：任务/图片/门禁/部分交付/被吞降级/在途/阶段耗时直方图/发起量/成本/LLM 用量；
- 多副本说明：这些是**本实例**的进程内计数（与 health 的 `scope=instance` 一致），
  跨副本汇总由 Prometheus 的 `sum by (instance)` 完成。
"""

from __future__ import annotations

import logging

logger = logging.getLogger("sku-agent.image.observability")

_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
_METRIC_PREFIX = "image"


def _escape_label(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _counter(lines: list[str], name: str, help_text: str, samples: list[tuple[str, float]]) -> None:
    if not samples:
        samples = [("", 0.0)]
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} counter")
    for label, value in samples:
        if label:
            lines.append(f'{name}{{{label}}} {value}')
        else:
            lines.append(f"{name} {value}")


def _gauge(lines: list[str], name: str, help_text: str, value: float) -> None:
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} gauge")
    lines.append(f"{name} {value}")


def _histogram(lines: list[str], name: str, help_text: str, stages: dict,
               bounds_ms: tuple[int, ...]) -> None:
    if not stages:
        return
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} histogram")
    for stage, entry in sorted(stages.items()):
        buckets = entry.get("buckets") or []
        for index, bound in enumerate(bounds_ms):
            count = sum(buckets[: index + 1])
            label = f'stage="{_escape_label(stage)}",le="{bound / 1000.0:.3f}"'
            lines.append(f"{name}_bucket{{{label}}} {count}")
        total = int(entry.get("count") or 0)
        label = f'stage="{_escape_label(stage)}",le="+Inf"'
        lines.append(f"{name}_bucket{{{label}}} {total}")
        total_ms = float(entry.get("sum") or 0.0)
        lines.append(f'{name}_sum{{stage="{_escape_label(stage)}"}} {total_ms / 1000.0:.6f}')
        lines.append(f'{name}_count{{stage="{_escape_label(stage)}"}} {total}')


def render_metrics() -> str:
    """渲染 Prometheus text format（优先官方库，缺失则用内置渲染器）。"""
    try:  # pragma: no cover —— 环境装了官方库才走这条
        from prometheus_client import REGISTRY, generate_latest

        if getattr(REGISTRY, "_image_custom_collector", None) is None:
            raise ImportError("custom collector missing")
        body = generate_latest(REGISTRY).decode("utf-8")
        if f"{_METRIC_PREFIX}_tasks_total" in body:
            return body
    except Exception:  # noqa: BLE001 —— 没装/没注册：用内置渲染器
        pass
    return _render_builtin()


def _render_builtin() -> str:
    from app.agents_image import metrics as image_metrics

    counters = image_metrics.snapshot()
    lines: list[str] = []

    _counter(lines, f"{_METRIC_PREFIX}_tasks_total", "生图任务计数（created/done/failed）", [
        ('status="created"', counters.get("tasks_created", 0)),
        ('status="done"', counters.get("tasks_done", 0)),
        ('status="failed"', counters.get("tasks_failed", 0)),
    ])
    _counter(lines, f"{_METRIC_PREFIX}_images_total", "图片张数（attempted/delivered）", [
        ('kind="attempted"', counters.get("images_attempted", 0)),
        ('kind="delivered"', counters.get("images_delivered", 0)),
    ])
    _counter(lines, f"{_METRIC_PREFIX}_provider_calls_total", "第三方生图调用发起次数（含重试）",
             [("", counters.get("provider_calls", 0))])
    _counter(lines, f"{_METRIC_PREFIX}_gate_skipped_total", "像素门禁跳过次数（非 PNG / 解码失败）",
             [("", counters.get("gate_skipped", 0))])
    _counter(lines, f"{_METRIC_PREFIX}_partial_deliveries_total", "部分成功任务数",
             [("", counters.get("partial_deliveries", 0))])
    swallowed = counters.get("swallowed") or {}
    _counter(lines, f"{_METRIC_PREFIX}_swallowed_total", "被安静吞掉的降级次数",
             [(f'kind="{_escape_label(kind)}"', value) for kind, value in sorted(swallowed.items())])
    _counter(lines, f"{_METRIC_PREFIX}_llm_calls_total", "图像解析/评审的 LLM 调用次数",
             [("", counters.get("llm_calls", 0))])
    _counter(lines, f"{_METRIC_PREFIX}_llm_tokens_total", "LLM token 用量",
             [("", counters.get("llm_tokens", 0))])
    _counter(lines, f"{_METRIC_PREFIX}_cost_cny_total", "provider 真实计费（元）",
             [("", round(float(counters.get("cost_cny", 0.0) or 0.0), 6))])
    _gauge(lines, f"{_METRIC_PREFIX}_in_flight", "本实例正在生成的图片数",
           counters.get("in_flight_images", 0))
    # P2-4（第十一轮）：把同步 IO 卸载到线程池之后，池子打满只会表现为"变慢"，
    # 指标上看不出来。这里导出线程池的调用数/在飞/占用时长，便于定位排队。
    from app.observability.trace import threadpool_stats

    pool = threadpool_stats()
    _counter(lines, f"{_METRIC_PREFIX}_threadpool_calls_total",
             "同步 DB/IO 卸载到线程池的调用次数",
             [("", pool.get("calls", 0))])
    _counter(lines, f"{_METRIC_PREFIX}_threadpool_busy_seconds_total",
             "线程池累计占用时长（含排队等待，秒）",
             [("", round(float(pool.get("busy_seconds", 0.0) or 0.0), 6))])
    _gauge(lines, f"{_METRIC_PREFIX}_threadpool_in_flight",
           "线程池中正在执行/排队等待的任务数", pool.get("in_flight", 0))
    _gauge(lines, f"{_METRIC_PREFIX}_threadpool_busy_seconds_max",
           "单次线程池任务最长占用时长（秒）",
           round(float(pool.get("busy_max_seconds", 0.0) or 0.0), 6))
    _histogram(lines, f"{_METRIC_PREFIX}_stage_seconds", "链路阶段耗时（秒）",
               image_metrics.stage_snapshot(), image_metrics.stage_bounds_ms())
    return "\n".join(lines) + "\n"


__all__ = ["render_metrics", "_CONTENT_TYPE"]
