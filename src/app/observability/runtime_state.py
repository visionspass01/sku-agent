"""运行期小状态：看板要展示的"当前正在发生什么"（如对话 SSE 进行中数量）。

刻意保持轻量：只用进程内原子计数，不引入额外依赖；多进程部署时各进程独立计数。
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_active_chat_streams = 0


def chat_stream_started() -> int:
    global _active_chat_streams
    with _lock:
        _active_chat_streams += 1
        return _active_chat_streams


def chat_stream_finished() -> int:
    global _active_chat_streams
    with _lock:
        _active_chat_streams = max(0, _active_chat_streams - 1)
        return _active_chat_streams


def active_chat_streams() -> int:
    with _lock:
        return _active_chat_streams


def reset() -> None:
    """测试用。"""
    global _active_chat_streams
    with _lock:
        _active_chat_streams = 0
