"""测试用 FakeLLM：按 schema 返回合法结构化输出，无需真实调用大模型。"""

from __future__ import annotations

import re

from pydantic import BaseModel


class FakeLLM:
    """实现与 LLMClient 相同的 `invoke_json` 接口，供图集成测试使用。"""

    def __init__(self, passed: bool = True, supervisor_tasks: list[str] | None = None,
                 passed_sequence: list[bool] | None = None) -> None:
        self.passed = passed
        # 复审 F3：按顺序给 VerifierVerdict 不同结论（例：[False, True] = 先失败、重写后通过），
        # 用于验证"追问/首轮校验失败会回到 reasoner 重写"。
        self.passed_sequence = list(passed_sequence or [])
        self.supervisor_tasks = supervisor_tasks or ["erp", "market", "rag"]
        self.stats = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "errors": 0}
        self.prompts: list[str] = []   # 所有 invoke_json 的 user prompt（断言注入用）
        self.schema_calls: list[str] = []   # 按顺序记录被请求的 schema 名（断言调用次数）
        self.last_user = ""            # 最近一次（兼容旧断言）

    async def invoke_json(self, system: str, user: str, schema: type[BaseModel], max_tokens: int | None = None):
        self.stats["calls"] += 1
        self.last_user = user
        self.prompts.append(user)
        name = schema.__name__
        self.schema_calls.append(name)
        if name == "SupervisorPlan":
            return schema(tasks=list(self.supervisor_tasks), priority="中", note="测试派发")
        if name == "InspectionPlan":
            # chunk_id 现在是内容寻址（`doc#hash8`），正则同时兼容旧的 `doc#001`
            ids = re.findall(r"\[([a-z0-9\-]+#[0-9a-z]{2,})\]", user)
            m = re.search(r'"suggested_qty":\s*(\d+)', user)
            suggested = int(m.group(1)) if m else 0
            if suggested > 0:
                action = {"action": "补货", "qty": suggested,
                          "reason": f"按补货测算建议 {suggested} 件（测试）", "eta_days": 7}
            else:
                action = {"action": "观察", "qty": None, "reason": "覆盖天数已满足（测试）", "eta_days": None}
            return schema(
                risk_level="中",
                verdict="测试结论",
                evidence=["库存与销量数据见数据包", "补货测算建议见 Skill 输出", "供应商交期见数据包"],
                plan=[action],
                citations=ids[:2],
            )
        if name == "VerifierVerdict":
            passed = (self.passed_sequence.pop(0) if self.passed_sequence else self.passed)
            return schema(passed=passed, issues=[] if passed else ["测试未通过"],
                          fixes=[], compliance={})
        raise AssertionError(f"FakeLLM 未覆盖的 schema: {name}")

    async def invoke_text(self, system: str, user: str, max_tokens: int | None = None) -> str:
        self.stats["calls"] += 1
        return "ok"
