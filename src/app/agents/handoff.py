"""Agent 间的交接契约（handoff contract）最小实现。

为什么需要：JD 里反复出现 "inter-agent handoff contract / 跨 Agent 状态同步"。
本项目的编排是确定性的（Supervisor 定采集子集 → Worker 并发 → Reasoner → Verifier），
不需要 A2A 协议那套通用握手；真正需要的是「交接时说清楚：谁给谁、为什么、给了什么、
期望拿回什么」，并且**能被 trace 记录**，出了问题能复盘。

设计取舍：
- 不做完整 A2A 协议（能力发现 / 任务协商 / 跨进程消息）：单进程图式编排里收益为负；
- 只做「结构化交接单」：字段固定 → 可校验（缺字段直接报错，而不是悄悄传 None）；
- `to_trace()` 输出到 langgraph state["trace"]，前端「执行轨迹」里能直接看到 handoff。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

AgentName = Literal["supervisor", "collector", "reasoner", "verifier", "chat_agent"]


class HandoffError(ValueError):
    """交接单不完整/不自洽（拿不到必需字段就拒绝执行，而不是带着空值往下跑）。"""


@dataclass(frozen=True)
class HandoffContract:
    """一次 Agent 交接：谁 → 谁、为什么、带什么数据、期望什么结果。"""

    from_agent: AgentName
    to_agent: AgentName
    reason: str
    payload: dict[str, Any] = field(default_factory=dict)
    expected: str = ""            # 期望对方产出什么（一句话，便于 verifier 对照）
    trace_id: str = ""            # 关联的 run/thread，便于串起来看

    def validate(self) -> None:
        if self.from_agent == self.to_agent:
            raise HandoffError("交接双方不能是同一个 Agent")
        if not (self.reason or "").strip():
            raise HandoffError("handoff 必须写明 reason（为什么交给对方）")
        if not (self.expected or "").strip():
            raise HandoffError("handoff 必须写明 expected（期望对方产出什么）")
        if not isinstance(self.payload, dict):
            raise HandoffError("payload 必须是结构化字段（dict），不要传整段自然语言")

    def to_trace(self, elapsed_ms: int | None = None) -> dict:
        """转成图 trace 的一条（与其它节点 trace 同构，前端可直接渲染）。"""
        self.validate()
        item = {
            "node": f"handoff:{self.from_agent}->{self.to_agent}",
            "status": "ok",
            "ms": int(elapsed_ms or 0),
            "detail": f"{self.reason}｜期望：{self.expected}｜字段：{sorted(self.payload)}",
        }
        if self.trace_id:
            item["trace_id"] = self.trace_id
        return item


def start(from_agent: AgentName, to_agent: AgentName, reason: str,
          payload: dict[str, Any] | None = None, expected: str = "",
          trace_id: str = "") -> HandoffContract:
    """构造并校验一份交接单（不合法立刻抛错）。"""
    contract = HandoffContract(
        from_agent=from_agent, to_agent=to_agent, reason=reason,
        payload=dict(payload or {}), expected=expected, trace_id=trace_id,
    )
    contract.validate()
    return contract


def finish(contract: HandoffContract, started_at: float | None = None) -> dict:
    """交接完成时返回 trace 条目（耗时 = 从 start 到现在）。"""
    elapsed = int((time.monotonic() - started_at) * 1000) if started_at else 0
    return contract.to_trace(elapsed_ms=elapsed)
