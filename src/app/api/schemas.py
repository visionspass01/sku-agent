"""FastAPI 请求/响应模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class InspectRequest(BaseModel):
    sku_id: str = Field(description="SKU 编码，如 ST-05-C1-M")
    as_of: str | None = Field(default=None, description="业务日期 YYYY-MM-DD，默认今天")


class FollowupRequest(BaseModel):
    """多轮追问：复用同一分析 thread 的上一轮采集数据，只重跑研判（不重复取数）。"""

    sku_id: str = Field(description="SKU 编码（thread_id 缺省时拼 `sku:<sku_id>`）")
    instruction: str = Field(min_length=1, max_length=500,
                             description="追问指令，如「那优先调拨还是补货？」")
    thread_id: str | None = Field(default=None, description="分析线程 id，缺省 sku:<sku_id>")


class LoginRequest(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class LoginResponse(BaseModel):
    token: str
    user: dict


class SecurityRecord(BaseModel):
    username: str
    ip: str
    at: str


class SecurityRecordsResponse(BaseModel):
    window_hours: int = 4
    note: str = "仅保留近 4 小时登录记录"
    records: list[dict] = []


class InspectStartResponse(BaseModel):
    run_id: str
    status: str = "running"


class PatrolRequest(BaseModel):
    mode: Literal["hourly", "daily"] = "daily"
    limit: int | None = Field(default=20, ge=1, le=200)


class PatrolResponse(BaseModel):
    run_id: str
    status: str = "running"


class RunStatusResponse(BaseModel):
    run_id: str
    kind: str
    status: str
    stats: dict = {}
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    results: list[dict] = []
    report: dict = {}
    progress: dict = {}
