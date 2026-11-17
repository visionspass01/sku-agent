"""模型可见的工具面（复审 A1）：描述与参数说明必须真的进上下文。

背景：`StructuredTool.from_function(description=...)` 会丢掉函数 docstring，
而按签名推断的 args_schema 只有字段名与类型 —— 模型看不到 `topk/days/qty/reason`
的含义，`save_analysis` 的"仅在用户明确要求沉淀时调用"更是完全不可见。
这里把"模型能看到什么"钉成断言：谁把描述删了/把 schema 写漂了，测试就会红。
"""

from __future__ import annotations

import inspect

from app.auth import AuthUser
from app.tools.platform_tools import (
    PARAM_DOCS,
    WRITE_TOOL_NAMES,
    build_platform_tools,
    expected_tool_count,
)

# 写工具的描述里必须出现"什么时候该调"的信号（避免模型顺手建单/顺手留档）
WRITE_POLICY_HINTS = ("仅", "人工确认", "不要", "不会自动")


def _tools():
    return build_platform_tools(AuthUser(username="admin", role="admin", department=None))


def _props(tool) -> dict:
    if tool.args_schema is None:
        return {}
    return tool.args_schema.model_json_schema().get("properties") or {}


def test_tool_count_matches_expected():
    """装配数与 `expected_tool_count` 同源（历史上这里漂过一次：日志 0 个、实际 22 个）。"""
    assert len(_tools()) == expected_tool_count(["*"])


def test_every_tool_has_meaningful_description():
    thin = [(t.name, t.description) for t in _tools() if len((t.description or "").strip()) < 12]
    assert thin == [], thin


def test_every_parameter_has_description():
    """参数说明是"模型知道怎么填"的唯一依据：缺失即视为工具面退化。"""
    missing: list[str] = []
    for tool in _tools():
        for name, spec in _props(tool).items():
            if not str(spec.get("description") or "").strip():
                missing.append(f"{tool.name}.{name}")
    assert missing == [], f"缺少参数说明：{missing}"


def test_args_schema_matches_the_real_signature():
    """参数集合必须与实现签名一致（防止 schema 与函数漂移）。"""
    for tool in _tools():
        signature = inspect.signature(tool.coroutine)
        expected = {
            name for name, param in signature.parameters.items()
            if name not in ("self", "cls")
            and param.kind not in (param.VAR_POSITIONAL, param.VAR_KEYWORD)
        }
        assert set(_props(tool)) == expected, tool.name


def test_write_tools_carry_usage_policy():
    """写工具的描述必须写清"何时该调/不自动审批"，不能只写"它能干什么"。"""
    tools = {t.name: t for t in _tools()}
    for name in sorted(WRITE_TOOL_NAMES):
        desc = tools[name].description or ""
        assert any(hint in desc for hint in WRITE_POLICY_HINTS), (name, desc)

    save = tools["save_analysis"].description
    assert "仅" in save and any(word in save for word in ("沉淀", "留档", "存档")), save


def test_write_tool_descriptions_are_not_shared_with_read_tools():
    """写工具的描述不能与只读工具雷同（避免"看起来都能调"）。"""
    tools = {t.name: t for t in _tools()}
    read_keys = {t.description for t in tools.values() if t.name not in WRITE_TOOL_NAMES}
    for name in sorted(WRITE_TOOL_NAMES):
        assert tools[name].description not in read_keys


def test_param_docs_are_non_trivial():
    """共享参数说明表本身不能是空话（挡住以后有人塞个空串了事）。"""
    for name, text in PARAM_DOCS.items():
        assert len(text.strip()) >= 6, (name, text)


def test_read_only_agent_tools_have_no_write_tools_but_keep_descriptions(seeded):
    """只读 Agent 的工具集被剔除写工具后，描述仍在（剔除不破坏工具面）。"""
    from app.agents.registry import get_registry

    registry = get_registry()
    registry.reload()
    manifest = registry.get("diagnostician")
    assert manifest is not None
    tools = build_platform_tools(
        AuthUser(username="worker1", role="user", department="男装"),
        allow=list(manifest.tool_names),
    )
    names = {t.name for t in tools}
    assert names & WRITE_TOOL_NAMES == set()
    assert all((t.description or "").strip() for t in tools)
