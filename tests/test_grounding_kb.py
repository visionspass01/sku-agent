"""知识库回答的溯源校验（评审 v3-②）。

暴露过的问题（面试官一问就中）：
1. `run_detail` 的工具结果统一截 500 字 —— SOP 的阈值/天数常在第 3、4 段，
   截掉之后「有依据的回答」被出口闸门判成「数字无处可溯」，最后给用户挂上
   「⚠️ 自检未通过」，等于自己打自己；
2. `grounding` 只认 `res_...` 形式的取数结果 id，不认知识库的 `chunk_id`
   （`doc_id#sha1[:8]`）。于是「按 SOP 的 60 天清仓」这种**有出处**的引用
   被判成"编造编号"，而提示词里恰恰要求模型标 chunk_id。

这里把两条都钉住：知识库证据不被截断、chunk_id 是合法来源，且反例（编造的
chunk_id）仍然会被抓出来——放松校验不能变成后门。
"""

from __future__ import annotations

import asyncio

import pytest

from app.agents.chat_agent import KB_TOOL_NAMES, _trace_result_limit
from app.agents.grounding import (
    CHUNK_ID_RE,
    check_reply,
    collect_result_ids,
    extract_claims,
    ground_reply,
    tool_result_text,
)

CHUNK_ID = "sop-clearance-general#a1b2c3d4"
KB_RESULT = (
    '{"query": "清仓规则", "count": 1, "no_answer": false, "hits": [{"chunk_id": "'
    + CHUNK_ID
    + '", "title": "清仓 SOP", "section": "触发条件", "text": "当季款售罄率 ≥85% 或上架超过 '
    "60 天进入清仓；清仓不做补货，改走折扣。" + '"}]}'
)


@pytest.fixture()
def grounding_on(_settings, monkeypatch):
    """conftest 默认关掉出口校验（避免测试打真实模型），这里按需打开。"""
    monkeypatch.setattr(_settings, "grounding_enabled", True)


def _trace(result: str, tool: str = "search_knowledge") -> list[dict]:
    return [{"type": "call", "tool": tool, "args": {}},
            {"type": "result", "tool": tool, "result": result}]


class TestChunkIdCitations:
    def test_chunk_id_regex_matches_retriever_ids(self):
        assert CHUNK_ID_RE.findall(f"参考 {CHUNK_ID} 的口径") == [CHUNK_ID]
        assert CHUNK_ID_RE.findall("sop-sizegap#deadbeef") == ["sop-sizegap#deadbeef"]
        # 不是 chunk_id 的普通文本不能被抓成来源
        assert CHUNK_ID_RE.findall("售罄率 85% 或 60 天") == []

    def test_chunk_id_counted_as_valid_source(self):
        reply = f"当季款上架超过 60 天或售罄率 ≥85% 进入清仓（{CHUNK_ID}）。"
        facts = check_reply(reply, result_ids=collect_result_ids([], _trace(KB_RESULT)),
                            tool_text=tool_result_text(_trace(KB_RESULT)), tool_calls=1)
        assert facts["passed"], facts["issues"]
        assert facts["citations"]["chunk_ids"] == [CHUNK_ID]
        assert facts["citations"]["result_ids"] == []

    def test_unknown_chunk_id_still_flagged(self):
        fake = "sop-clearance-general#ffffffff"
        reply = f"清仓阈值是 60 天（{fake}）。"
        facts = check_reply(reply, result_ids=collect_result_ids([], _trace(KB_RESULT)),
                            tool_text=tool_result_text(_trace(KB_RESULT)), tool_calls=1)
        assert not facts["passed"]
        assert any("不存在" in i for i in facts["issues"])
        assert facts["citations"]["unknown"] == [fake]

    def test_missing_citation_still_flagged(self):
        """有了 chunk_id 这条通道，也不能让"什么都不标"过关。"""
        facts = check_reply("当季款上架超过 60 天就要清仓。",
                            result_ids=collect_result_ids([], _trace(KB_RESULT)),
                            tool_text=tool_result_text(_trace(KB_RESULT)), tool_calls=1)
        assert not facts["passed"]
        assert any("没有标注任何" in i for i in facts["issues"])


class TestKbEvidenceNotTruncated:
    def test_kb_tool_gets_large_limit(self):
        assert "search_knowledge" in KB_TOOL_NAMES
        assert _trace_result_limit("search_knowledge") == 6000
        assert _trace_result_limit("get_inventory") == 500

    def test_limit_is_configurable(self, _settings, monkeypatch):
        monkeypatch.setattr(_settings, "kb_evidence_max_chars", 12000)
        assert _trace_result_limit("search_knowledge", _settings) == 12000
        # 下限保护：配得再小也不低于普通工具的 500，避免"越配越丢证据"
        monkeypatch.setattr(_settings, "kb_evidence_max_chars", 10)
        assert _trace_result_limit("search_knowledge", _settings) == 500

    def test_number_beyond_500_chars_is_still_grounded(self):
        """SOP 尾部的阈值（第 1200 字之后）：500 字版本判编造，按 6000 字保留则能过关。"""
        filler = "前置说明。" * 300        # ≈1500 字
        long_result = KB_RESULT.replace(
            "改走折扣", "改走折扣；清仓阈值 45 天，齐码率 85%，售罄率 90%",
        ).replace('"text": "', '"text": "' + filler)
        assert len(long_result) > 1200
        reply = f"清仓阈值 45 天（{CHUNK_ID}），齐码率 85%，售罄率 90%。"
        numbers = extract_claims(reply)
        assert numbers == ["45天", "85%", "90%"]

        kept = check_reply(reply, result_ids=collect_result_ids([], _trace(long_result)),
                           tool_text=tool_result_text(_trace(long_result)), tool_calls=1)
        assert kept["passed"], kept["issues"]

        truncated = _trace(long_result[:500])
        cut = check_reply(reply, result_ids=collect_result_ids([], truncated),
                          tool_text=tool_result_text(truncated), tool_calls=1)
        assert not cut["passed"], "500 字版本本来就该失败——这正是修复前的现象"
        assert cut["numbers"]["unverified_count"] == 3

    def test_tool_result_text_falls_back_to_evidence(self):
        trace = [{"type": "result", "tool": "search_knowledge", "evidence": f"阈值 60 天（{CHUNK_ID}）"}]
        assert "60 天" in tool_result_text(trace)
        assert CHUNK_ID in collect_result_ids([], trace)


class TestGroundReplyWithKb:
    def test_ground_reply_passes_kb_answer(self, grounding_on):
        """端到端：知识库回答 + chunk_id → status=passed（不重写、不挂警告）。"""

        async def run():
            return await ground_reply(
                f"当季款上架超过 60 天进入清仓（{CHUNK_ID}）。",
                steps=[], tool_trace=_trace(KB_RESULT),
            )

        out = asyncio.run(run())
        assert out["report"]["status"] == "passed"
        assert out["report"]["citations"]["chunk_ids"] == [CHUNK_ID]
        assert "自检未通过" not in out["reply"]

    def test_ground_reply_degrades_on_fabricated_chunk(self, grounding_on, monkeypatch):
        """编造 chunk_id 且重写不可用 → 必须留痕（degraded + 警告块），不能静默放过。"""
        from app.agents import grounding
        from app.llm import LLMError

        class _LLM:
            async def invoke_text(self, system, user, max_tokens=None):
                raise LLMError("未配置 DEEPSEEK_API_KEY")

        monkeypatch.setattr(grounding, "get_llm", lambda: _LLM())
        out = asyncio.run(grounding.ground_reply(
            "清仓阈值是 45 天（sop-nope#deadbeef）。", steps=[], tool_trace=_trace(KB_RESULT),
        ))
        assert out["report"]["status"] == "degraded"
        assert out["report"]["citations"]["unknown"] == ["sop-nope#deadbeef"]
        assert "自检未通过" in out["reply"]


@pytest.mark.parametrize("claim,expected", [
    ("可用库存 42 件", ["42件"]),
    ("售罄率 85%", ["85%"]),
    ("超过 60 天", ["60天"]),
    ("折扣 7.5 折", ["7.5折"]),
])
def test_extract_claims_unchanged(claim, expected):
    """知识库口径里的数字同样是"带单位数字"，解析规则保持一致。"""
    assert extract_claims(claim) == expected
