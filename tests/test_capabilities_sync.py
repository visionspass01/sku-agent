"""能力目录一致性（评审 C2 / 2.1-2 / 2.1-3）。

守三件事：
1. 仓库里的快照 == 中台 `data_platform/capabilities.py`（改漏一边即失败）；
2. Agent 侧 `TOOL_GROUPS` / `TOOL_SCOPES` 完全由快照派生（不许手写第二份真相）；
3. 目录声明的工具与 `build_platform_tools` 真正装配出来的工具**逐个对齐**
   （否则"目录里有、装配时没有"会让模型看到不存在的工具）。
"""

from __future__ import annotations

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "src" / "app" / "tools" / "capabilities.snapshot.json"


def test_platform_catalog_self_check_passes():
    from data_platform import capabilities

    assert capabilities.validate() == []


def test_snapshot_matches_platform_module():
    from data_platform.capabilities import RESERVED_GROUPS, catalog

    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    remote = catalog()
    assert snapshot["version"] == remote["version"], "快照版本落后于中台"
    assert snapshot["groups"] == remote["groups"], "分组不一致：请跑 gen_capabilities_snapshot.py"
    assert snapshot["capabilities"] == remote["capabilities"], "能力明细不一致"
    assert snapshot["reserved_groups"] == RESERVED_GROUPS


def test_agent_tool_tables_are_derived_from_snapshot():
    from app.tools import platform_tools as pt
    from data_platform.capabilities import groups, scope_kinds

    assert pt.TOOL_GROUPS == groups()
    assert pt.TOOL_SCOPES == scope_kinds()
    assert pt.ALL_TOOL_NAMES == {name for names in groups().values() for name in names}
    assert pt.RESERVED_GROUPS, "预留分组必须显式声明（image 组尚未实现）"


def test_no_group_is_empty(  ):
    from app.tools import platform_tools as pt

    empty = [group for group, names in pt.TOOL_GROUPS.items() if not names]
    assert not empty, f"空分组会让 tools:[该组] 解析出空集：{empty}"


def test_reserved_group_is_not_usable():
    from app.tools import platform_tools as pt

    assert "image" in pt.RESERVED_GROUPS
    assert "image" not in pt.TOOL_GROUPS


def test_catalog_tools_equal_assembled_tools():
    """能力目录 ↔ 真实装配：多一个少一个都要失败。"""
    from app.tools import platform_tools as pt

    tools = pt.build_platform_tools(None)
    names = {tool.name for tool in tools}
    assert names == pt.ALL_TOOL_NAMES, {
        "catalog_only": sorted(pt.ALL_TOOL_NAMES - names),
        "assembled_only": sorted(names - pt.ALL_TOOL_NAMES),
    }
    assert len(tools) == len(pt.ALL_TOOL_NAMES)


def test_capability_status_shape():
    from app.tools import platform_tools as pt

    status = pt.capability_status()
    assert status["tools"] == len(pt.ALL_TOOL_NAMES)
    assert status["groups"] == len(pt.TOOL_GROUPS)
    assert "snapshot_version" in status and "drift" in status
