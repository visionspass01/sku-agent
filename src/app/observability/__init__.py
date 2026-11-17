"""可观测性：延迟采样、事件流等运行期指标 + 全链路 trace。"""

from app.observability import trace  # noqa: F401 —— 常用入口，避免各处写完整路径

__all__ = ["trace"]
