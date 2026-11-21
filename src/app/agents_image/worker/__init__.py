"""独立生图调用 worker 包（可跑成本地 CLI / HTTP 服务 / 腾讯云 SCF 函数）。

入口：
- `python -m app.agents_image.worker.local_main --input req.json --output out.json`
- `uvicorn app.agents_image.worker.http_app:app`（`POST /generate`，Bearer `IMAGE_WORKER_TOKEN`）
- `app.agents_image.worker.scf_main.main_handler(event, context)`
"""

from __future__ import annotations

from app.agents_image.worker.contract import (
    WORKER_CONTRACT_VERSION,
    WorkerError,
    WorkerImage,
    WorkerRef,
    WorkerRequest,
    WorkerResult,
    WorkerRules,
    WorkerSlot,
)
from app.agents_image.worker.runner import execute, result_to_json

__all__ = [
    "WORKER_CONTRACT_VERSION",
    "WorkerRequest", "WorkerResult", "WorkerSlot", "WorkerRef", "WorkerRules",
    "WorkerImage", "WorkerError",
    "execute", "result_to_json",
]
