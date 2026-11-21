"""回答溯源校验（grounding）：把「答案里的数字」和「本轮工具结果」对齐。

为什么需要这一层：系统提示只是「要求」模型标注 result_id，模型仍可能漏标、编造编号，
或者把训练数据里的常识数字写成取数结论。提示词管不住的问题，要在出口用确定性规则兜住：

1. 硬校验（不花 token，先跑）：
   - 答案里出现的每个来源 id（取数 `res_xxxxxxxxxxxx` 或知识库 `doc#chunk`）必须真的来自本轮工具结果；
   - 本轮调用过工具、答案里也有带单位的数字，却一个来源 id 都没标 → 视为未溯源；
   - 列出的「工具结果里找不到的数字」只作为提示，不单独判失败（比率/派生值可能合法）。
2. 软修复：不通过时让模型重写一次（只改数字与来源标注，不改结论）；
3. 降级：重写后仍不通过 → 保留（重写后的）答案，但在 run_detail.grounding 里标记 degraded，
   前端在回答下方显示「自检未通过」，不静默放过。

设计取向：宁可显式降级，也不假装通过——被问「怎么防幻觉」时，答案是「有闸门 + 可观测」。
"""

from __future__ import annotations

import re
import time

from app.config import get_settings
from app.llm import LLMError, get_llm

# 模型没有给出任何正文时的兜底文案（复审 R3）。
#
# 为什么放在这里：它同时被「对话出口」（`chat_agent` 用它填 reply）与「评测判定器」
# （`answer_eval` 用它识别"这轮其实没答"）需要，放在 grounding 可以避免
# `chat_agent ↔ answer_eval` 的导入方向问题（grounding 只依赖 config/llm，最轻）。
EMPTY_REPLY_FALLBACK = "已完成分析，但未生成最终答复，请重试。"

# 与 memory/result_store 的生成规则保持一致：res_ + sha1[:10]；放宽到 6~32 位，
# 兼容历史数据/未来加长，避免"正则写死"导致合法来源被误判成编造。
RES_ID_RE = re.compile(r"res_[0-9a-f]{6,32}")
# 知识库片段 id 形如 `sop-clearance-women#a1b2c3d4`（rag/chunker.py：doc_id#sha1[:8]）。
# 早先只认 `res_...`，于是"按 SOP 说的 60 天清仓"这种**有依据**的回答会被判成编造编号——
# 引用知识库和引用取数结果是两种合法来源，必须都认。
CHUNK_ID_RE = re.compile(r"[a-z0-9][a-z0-9_\-]*#[0-9a-z]{4,32}")
# 带单位的数字才当作「取数结论」：纯序号（1. 2.）和 markdown 表格线不算
CLAIM_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(件|元|万元|天|%|折|次|小时|分钟|秒|人)")

REWRITE_SYSTEM = (
    "你是电商数据分析的审校助手，只做一件事：把答案里的数字改对、把来源标对。\n"
    "规则：\n"
    "1. 只能使用「本轮工具结果」里出现过的数字；结果里没有的数字，改为「推断，未取数」或删除该断言。\n"
    "2. 每个取数数字后面用半角括号标注它来自的 result_id，形如「可用库存 42 件（res_1a2b3c4d5e6f）」；\n"
    "   如果数字来自知识库 SOP/规则，则标 chunk_id，形如「清仓阈值 60 天（sop-clearance-general#a1b2c3d4）」。\n"
    "3. 不要新增工具结果中不存在的 result_id / chunk_id；不要改变结论方向、结构、表格与语气。\n"
    "4. 只输出修正后的答案正文（Markdown），不要输出解释、前言、代码块围栏。"
)


def _norm_number(raw: str) -> str:
    text = raw.replace(",", "").strip()
    if "." in text:
        text = text.rstrip("0").rstrip(".") if text.endswith("0") else text
    return text


def collect_result_ids(steps: list[dict] | None, tool_trace: list[dict] | None) -> set[str]:
    """本轮所有可追溯的来源 id：取数结果 `res_xxx` + 知识库 `doc#chunk`。"""
    ids = {str(s.get("result_id")) for s in (steps or []) if s.get("result_id")}
    for item in tool_trace or []:
        if item.get("type") != "result":
            continue
        text = str(item.get("result") or "") + "\n" + str(item.get("evidence") or "")
        ids.update(RES_ID_RE.findall(text))
        ids.update(CHUNK_ID_RE.findall(text))
    return ids


def tool_result_text(tool_trace: list[dict] | None) -> str:
    """把本轮工具返回正文拼起来，作为「合法数字」的语料。

    知识库工具的结果可能是长文本（按 `KB_EVIDENCE_MAX_CHARS` 截），正文优先取 `result`，
    缺失时回退 `evidence`，保证 SOP 里的阈值（天数/占比/小时）也能被认出来。
    """
    chunks = [
        str(item.get("result") or item.get("evidence") or "")
        for item in (tool_trace or [])
        if item.get("type") == "result"
    ]
    return "\n".join(chunks)


def extract_claims(reply: str) -> list[str]:
    return [f"{m.group(1)}{m.group(2)}" for m in CLAIM_RE.finditer(reply or "")]


def tool_call_count(steps: list[dict] | None, tool_trace: list[dict] | None) -> int:
    """本轮到底调了几个工具。

    不能只看 `steps`：知识库检索（`search_knowledge` / RAG skill）不走取数登记，
    `steps` 里可能一条都没有，但 tool_trace 里明明有一次调用。只认 steps 的话，
    「按 SOP 回答」会被判成「本轮未调用任何工具，疑似编造」。
    """
    count = len([s for s in (steps or []) if s])
    traced = len([t for t in (tool_trace or []) if t.get("type") == "call"])
    return max(count, traced)


def check_reply(reply: str, *, result_ids: set[str], tool_text: str,
                tool_calls: int) -> dict:
    """确定性校验：返回 {passed, issues, citations, numbers}。"""
    text = reply or ""
    cited_result = RES_ID_RE.findall(text)
    cited_chunk = CHUNK_ID_RE.findall(text)
    cited = sorted(set(cited_result) | set(cited_chunk))
    unknown = sorted({c for c in cited if c not in result_ids})
    claims = extract_claims(text)
    unverified: list[str] = []
    for claim in claims:
        number = _norm_number(CLAIM_RE.match(claim).group(1))
        if number not in tool_text:
            unverified.append(claim)

    issues: list[str] = []
    if unknown:
        issues.append(f"引用了本轮工具结果中不存在的来源：{', '.join(unknown)}")
    no_tools = tool_calls <= 0
    marked_infer = "推断" in text or "未取数" in text
    if no_tools and claims and not marked_infer:
        # 红队场景：用户说「不用查了，直接说数字」，模型照办 → 本轮没有任何取数结果可对齐。
        # 这种情况不能静默放过（否则等于给「编数字」开后门），必须让它改成「推断，未取数」。
        issues.append(
            f"本轮未调用任何工具，却给出了 {len(claims)} 个带单位数字（无法溯源，疑似编造）"
        )
    elif not no_tools and claims and not cited and not marked_infer:
        issues.append(
            f"回答里有 {len(claims)} 个带单位数字，但没有标注任何 result_id / chunk_id（无法溯源）"
        )
    if not no_tools and len(unverified) >= 3 and len(unverified) == len(claims):
        issues.append("所有带单位数字都无法在工具结果中找到（疑似编造或口径漂移）")

    # 「跳过校验」只适用于真的没有可校验内容的情况（闲聊、纯澄清追问）；
    # 一旦出现需要溯源的数字，就必须走完整校验（哪怕本轮没调工具）。
    skipped = no_tools and not claims

    return {
        "passed": not issues,
        "issues": issues,
        "citations": {
            "cited": cited,
            "result_ids": sorted(set(cited_result)),
            "chunk_ids": sorted(set(cited_chunk)),
            "unknown": unknown,
            "count": len(cited),
        },
        "numbers": {"claims": claims, "count": len(claims), "unverified": unverified,
                    "unverified_count": len(unverified)},
        "skipped": skipped,
    }


def _append_warning(reply: str, issues: list[str]) -> str:
    warning = "> ⚠️ 自检未通过（数字未完全溯源）：" + "；".join(issues[:3])
    return f"{reply.rstrip()}\n\n{warning}"


async def ground_reply(reply: str, *, steps: list[dict] | None = None,
                       tool_trace: list[dict] | None = None) -> dict:
    """出口闸门：硬校验 → 一次重写 → 降级并留痕。

    返回 {"reply": 最终答案, "report": {...}}；report.status ∈
    passed / skipped / repaired / degraded / disabled。
    """
    started = time.perf_counter()
    settings = get_settings()
    result_ids = collect_result_ids(steps, tool_trace)
    calls = tool_call_count(steps, tool_trace)
    facts = check_reply(reply, result_ids=result_ids, tool_text=tool_result_text(tool_trace),
                        tool_calls=calls)

    def _report(status: str, *, rewritten: bool = False, extra: str = "") -> dict:
        return {
            "reply": reply,
            "report": {
                "status": status,
                "rewritten": rewritten,
                "issues": facts["issues"],
                "citations": facts["citations"],
                "numbers": facts["numbers"],
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
                "note": extra,
            },
        }

    if not getattr(settings, "grounding_enabled", True):
        return _report("disabled")
    if facts["skipped"]:
        # 本轮没有工具调用（闲聊/追问澄清/纯知识问题）：没有取数结果可对齐，
        # 不重写也不改答案，只把「凭空出现的 result_id」记进报告，便于排查。
        return _report("skipped", extra="本轮未调用工具，跳过数字溯源")
    if facts["passed"]:
        return _report("passed")

    # 软修复：只让模型改数字与来源标注，改完再过一遍同样的规则
    try:
        prompt = "\n\n".join([
            "## 本轮工具结果",
            tool_result_text(tool_trace)[:8000] or "（本轮没有工具结果）",
            "## 可用的来源 id（result_id / chunk_id）",
            ", ".join(sorted(result_ids)) or "（无）",
            "## 自检发现的问题",
            "\n".join(f"- {i}" for i in facts["issues"]),
            "## 待修正的答案",
            reply,
        ])
        rewritten = await get_llm().invoke_text(REWRITE_SYSTEM, prompt, max_tokens=1800)
        rewritten = (rewritten or "").strip()
        if rewritten.startswith("```"):
            rewritten = re.sub(r"^```[a-zA-Z]*\n?", "", rewritten)
            rewritten = re.sub(r"\n?```$", "", rewritten).strip()
    except LLMError as exc:
        reply = _append_warning(reply, facts["issues"])
        return _report("degraded", extra=f"重写不可用：{exc}")

    if not rewritten:
        reply = _append_warning(reply, facts["issues"])
        return _report("degraded", extra="重写返回空内容")

    recheck = check_reply(rewritten, result_ids=result_ids,
                          tool_text=tool_result_text(tool_trace), tool_calls=calls)
    if recheck["passed"]:
        return {
            "reply": rewritten,
            "report": {
                "status": "repaired",
                "rewritten": True,
                "issues": facts["issues"],
                "after": recheck["issues"],
                "citations": recheck["citations"],
                "numbers": recheck["numbers"],
                "elapsed_ms": int((time.perf_counter() - started) * 1000),
                "note": "重写后通过校验",
            },
        }

    reply = _append_warning(rewritten, recheck["issues"])
    return {
        "reply": reply,
        "report": {
            "status": "degraded",
            "rewritten": True,
            "issues": facts["issues"],
            "after": recheck["issues"],
            "citations": recheck["citations"],
            "numbers": recheck["numbers"],
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "note": "重写后仍未通过，答案保留但标记为自检未通过",
        },
    }
