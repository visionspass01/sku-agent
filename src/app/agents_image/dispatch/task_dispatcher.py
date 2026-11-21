"""任务调度插件位：local（进程内本地函数）｜scf（腾讯云 SCF，接口就绪、后期启用）。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.core.errors import raise_image


@runtime_checkable
class TaskDispatcher(Protocol):
    name: str

    async def submit(self, task_id: str) -> str: ...
    async def cancel(self, task_id: str) -> bool: ...
    async def resume_pending(self) -> list[str]: ...
    async def health(self) -> dict: ...


_INSTANCES: dict[str, TaskDispatcher] = {}


def get_dispatcher(name: str | None = None) -> TaskDispatcher:
    """进程内单例：并发信号量与在途任务列表必须共享，否则每次请求都新开一套。"""
    from app.agents_image.config import get_image_settings
    from app.agents_image.dispatch.local_generator import LocalGenerator

    settings = get_image_settings()
    key = (name or settings.dispatcher or "local").strip().lower()
    if key == "local":
        if key not in _INSTANCES:
            _INSTANCES[key] = LocalGenerator()
        return _INSTANCES[key]
    if key == "scf":
        # 说明：目前只有协议位，没有 SCF 适配器实现（本轮改造会补齐）。
        # 文案不再指代不存在的文件，避免误导排障。
        raise_image("IMG_PROVIDER_UNAVAILABLE",
                    "SCF 调度器尚未实现（当前请把 IMAGE_DISPATCHER 设为 local）")
    raise_image("IMG_PROVIDER_UNAVAILABLE", f"未知的调度器：{key}")


def reset_dispatchers() -> None:
    """测试用：清掉单例（避免上一个用例的 asyncio 任务/信号量带到下一个用例）。"""
    for item in list(_INSTANCES.values()):
        reset = getattr(item, "reset", None)
        if callable(reset):
            reset()
    _INSTANCES.clear()
