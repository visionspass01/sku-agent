"""第六轮 P1-4：Critic 失败时的 KB 修复检索必须有相关性门槛。

上一版 `_repair_experience` 取 fixes 语料 top-1 就返回（无阈值、无类型匹配），
于是 aspect 失败会拿到"白底不纯"的修法、text_policy 失败会拿到"身份漂移"的修法，
把原本正确的内置规则顶掉；一旦碰巧通过还会被归档成错误经验（记忆自我放大）。
现在：按 issue 类型关键词做相关性匹配 + min_score 阈值，不相关就退回内置规则。
"""

from __future__ import annotations

import pytest

from app.agents_image import graph
from app.agents_image.rag import index as rag_index
from app.agents_image.state import ImageTaskState


@pytest.fixture(autouse=True)
def _reset_index():
    rag_index.reset_index()
    yield
    rag_index.reset_index()


def _state(product_type: str = "针织开衫（短款）") -> ImageTaskState:
    return ImageTaskState(task_id="t-repair", department="女装",
                          attrs={"product_type": product_type})


def test_fix_docs_exist_for_aspect_and_text_policy():
    docs = {doc.doc_id: doc for doc in rag_index.get_index().docs if doc.kind == "fix"}
    assert "fix-aspect-ratio" in docs
    assert "fix-text-layer" in docs


def test_aspect_failure_uses_aspect_fix_not_white_bg():
    state = _state()
    result = graph._repair_experience(state, "aspect|针织开衫（短款）")
    assert result["source"] == "kb", result
    patch = result["patches"]["aspect"]
    assert "比例" in patch or "画幅" in patch, patch
    assert "白底" not in patch, "不得把白底修法当成比例修法"
    assert any("IMG_REPAIR_KB: 命中知识库修复经验（fix-aspect-ratio" in item
               for item in state.warnings), state.warnings


def test_identity_failure_uses_identity_fix():
    state = _state("半身裙（长款）")
    result = graph._repair_experience(state, "identity|半身裙（长款）")
    assert result["source"] == "kb", result
    assert any("IMG_REPAIR_KB: 命中知识库修复经验（fix-identity-drift" in item
               for item in state.warnings), state.warnings
    assert "颜色" in result["patches"]["identity"]


def test_text_policy_failure_uses_text_fix():
    state = _state("连衣裙")
    result = graph._repair_experience(state, "text_policy|连衣裙")
    assert result["source"] == "kb", result
    patch = result["patches"]["text_policy"]
    assert "图内文字" in patch or "角标" in patch, patch
    assert any("fix-text-layer" in item for item in state.warnings), state.warnings


def test_kind_without_fix_doc_falls_back_to_builtin_rules():
    """没有对应 fix 文档的 issue 类型：不硬凑，退回内置规则（source 为空）。"""
    state = _state("冲锋衣（三合一）")
    result = graph._repair_experience(state, "mime|冲锋衣（三合一）")
    assert result == {"patches": {}, "source": ""}, result
    assert not any("IMG_REPAIR_KB" in item for item in state.warnings)


def test_fix_doc_matches_respects_kind_keywords():
    docs = {doc.doc_id: doc for doc in rag_index.get_index().docs if doc.kind == "fix"}
    assert graph._fix_doc_matches(docs["fix-aspect-ratio"], ["aspect"])
    assert not graph._fix_doc_matches(docs["fix-white-bg-dirty"], ["aspect"])
    assert graph._fix_doc_matches(docs["fix-white-bg-dirty"], ["identity"]) is False
    assert graph._fix_doc_matches(docs["fix-identity-drift"], ["identity"])
