"""四层记忆实现（对应 dsh 记忆机制的本地等价实现）。

- L0 工作记忆：`result_store.py` + `tool_contract.py`（工具返回 result_id/as_of/summary/artifact）
- L1 会话记忆：`session_memory.py`（<analysis-facts> 事实卡 + <working-set> 实体表）
- L2 工作区记忆：`workspace.py`（根 AGENTS.md 基线 + src/app/agents/skills 按需加载）
- L3 组织记忆：`org_memory.py`（analysis_result 表 + data/analyses/<部门>/ 档案）
"""

from app.memory.runtime import (
    chat_turn_context,
    current_chat_context,
    current_tool_context,
    tool_context,
)
from app.memory.tool_contract import with_result_contract

__all__ = [
    "chat_turn_context",
    "current_chat_context",
    "current_tool_context",
    "tool_context",
    "with_result_contract",
]
