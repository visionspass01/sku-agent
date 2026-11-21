"""独立 worker 的跨进程契约（v1）：入参/出参都是纯 JSON，进程内/HTTP/SCF 共用同一份。

设计要点：
- **入参带引用不带字节**：参考图用 `storage_key`（已存在的存储后端）或 `url` 传入，
  避免 SCF 事件 6MB 上限（6 张图 base64 直接超）；
- **出参带路径**：worker 出图后自己落存储（COS / 中台文件夹 / 内存），把
  `storage / storage_key / url / url_expire_at` 交回调用方，由父进程统一回写中台；
- **判定信息一并回传**：`gate`（门禁结论）与 `pixel`（像素统计）让父进程的 Critic
  不用再下载/解码图片（remote 模式下本地不留字节）；
- 版本号 `version` 参与校验：契约变更必须升版本，父/子两侧不匹配时快速失败。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

WORKER_CONTRACT_VERSION = "1"


class WorkerSlot(BaseModel):
    """一个图位的出图请求（与 `plan_slots` 的图位一一对应）。"""

    slot: int = Field(ge=1)
    prompt: str = ""
    negative_prompt: str = ""
    role: str = ""                       # 主图 / 场景 A …（门禁按角色决定是否校验）
    n: int = Field(default=1, ge=1, le=4)
    seed: int | None = None


class WorkerRef(BaseModel):
    """参考图引用：`storage_key`（同后端对象）/ `url`（可 GET 的地址）/ `inline`（base64，仅本地调试）。

    remote 部署（HTTP/SCF）用前两种，避免事件体积超限；`inline` 只给本地 CLI/测试省事。
    """

    kind: Literal["storage_key", "url", "inline"] = "storage_key"
    value: str = ""
    mime: str = "image/png"


class WorkerRules(BaseModel):
    """平台规则摘要（父进程从数据中台取到后原样下发；worker 不回调中台）。"""

    min_px: int = 800
    max_mb: float = 3.0
    white_ratio: float = 0.55
    subject_min: float = 0.15
    subject_max: float = 0.90
    margin_min: float = 0.15
    aspect_tolerance: float = 0.04
    aspect: str = "1:1"
    text_allowed: bool = True
    forbidden_words: list[str] = Field(default_factory=list)
    compliance: dict[str, Any] = Field(default_factory=dict)


class WorkerGate(BaseModel):
    """门禁参数（与 `compliance.gate` 的入参一一对应）。"""

    mode: str = "main"                   # main | all | off
    autofix: bool = True
    nonpng_policy: str = "warn"
    aspect: str = "1:1"


class WorkerStorageSpec(BaseModel):
    backend: str = ""                    # 空 = 用 worker 自己的 IMAGE_WORKER_STORAGE
    prefix: str = ""


class WorkerRequest(BaseModel):
    """一次"出图 + 落存储"的完整请求。"""

    version: str = WORKER_CONTRACT_VERSION
    task_id: str = ""
    trace_id: str = ""
    idempotency_key: str = ""            # 建议 task_id:slot:attempt；用于下游幂等
    provider: str = ""
    model: str = ""
    width: int = Field(default=1024, ge=64, le=4096)
    height: int = Field(default=1024, ge=64, le=4096)
    preserve: list[str] = Field(default_factory=list)
    slots: list[WorkerSlot] = Field(default_factory=list)
    refs: list[WorkerRef] = Field(default_factory=list)
    rules: WorkerRules = Field(default_factory=WorkerRules)
    gate: WorkerGate = Field(default_factory=WorkerGate)
    storage: WorkerStorageSpec = Field(default_factory=WorkerStorageSpec)
    deadline_at: float = 0.0             # epoch 秒；0 = 不设任务级截止
    return_bytes: bool = False           # 调试/本地模式可让 worker 回传字节（remote 不用）

    @field_validator("version")
    @classmethod
    def _version_ok(cls, value: str) -> str:
        if str(value or "") != WORKER_CONTRACT_VERSION:
            raise ValueError(f"worker 契约版本不匹配：期望 {WORKER_CONTRACT_VERSION}，收到 {value!r}")
        return str(value)

    @field_validator("slots")
    @classmethod
    def _slots_not_empty(cls, value: list[WorkerSlot]) -> list[WorkerSlot]:
        if not value:
            raise ValueError("slots 不能为空")
        return value


class SlotGate(BaseModel):
    passed: bool = True
    reasons: list[str] = Field(default_factory=list)
    fixed: bool = False
    degraded: bool = False
    stats: dict[str, Any] = Field(default_factory=dict)


class SlotPixel(BaseModel):
    dominant_rgb: list[int] | None = None
    white_ratio: float = 0.0
    subject_ratio: float = 0.0
    margin_ratio: float = 0.0
    width: int = 0
    height: int = 0
    pixel_check: bool = False


class WorkerImage(BaseModel):
    """一张已落存储的出图（父进程据此登记资产 + 回写中台路径）。"""

    slot: int
    kind: str = "output"                 # output | blocked
    sha256: str = ""
    mime: str = "image/png"
    width: int = 0
    height: int = 0
    bytes: int = 0
    storage: str = ""
    storage_key: str = ""
    url: str = ""
    url_expire_at: str = ""
    gate: SlotGate = Field(default_factory=SlotGate)
    pixel: SlotPixel = Field(default_factory=SlotPixel)
    blocked_reason: str = ""
    cost_cny: float = 0.0
    latency_ms: int = 0
    content: str = ""                    # 仅 return_bytes=True 时携带（base64）


class WorkerError(BaseModel):
    code: str = ""
    detail: str = ""
    retryable: bool = False


class WorkerResult(BaseModel):
    version: str = WORKER_CONTRACT_VERSION
    task_id: str = ""
    ok: bool = True
    images: list[WorkerImage] = Field(default_factory=list)
    provider_calls: int = 0
    generated: int = 0
    cost_cny: float = 0.0
    error: WorkerError | None = None
    timings: dict[str, int] = Field(default_factory=dict)

    @property
    def delivered(self) -> int:
        return sum(1 for item in self.images if item.kind == "output")

    @property
    def blocked(self) -> int:
        return sum(1 for item in self.images if item.kind == "blocked")


__all__ = [
    "WORKER_CONTRACT_VERSION",
    "WorkerSlot", "WorkerRef", "WorkerRules", "WorkerGate", "WorkerStorageSpec",
    "WorkerRequest", "SlotGate", "SlotPixel", "WorkerImage", "WorkerError",
    "WorkerResult",
]
