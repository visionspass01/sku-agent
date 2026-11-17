"""Skills 文案必须与代码口径同源（复审 R4）。

为什么需要：`weekly-ops-review` 之外，模型会按 `diagnostician.md` 的指引
`load_skill("size-gap-diagnosis")` —— skills/*.md 是**模型可见的提示词**，一旦里面的
口径与权威实现不一致（例如"同款零库存码数 ≥ 2 就算断码"），模型就会给出与治理口径相反的
结论，而数字是真的、`grounding` 拦不住。这里把关键口径钉成断言。

权威来源：
- 断码 / 主销码 / 齐码：`AGENTS.md`、`src/app/skills/inventory_judge.py`、`get_style_metrics`
- 补货公式：`src/app/skills/replenishment.py`
- 清仓折扣与分批：`prompts/chat_system.md`、`agents/verifier.py`
"""

from __future__ import annotations

from app.agents.prompt_loader import PROMPT_DIR
from app.tools.platform_tools import ALL_TOOL_NAMES

SKILLS = PROMPT_DIR.parent / "skills"


def _text(name: str) -> str:
    return (SKILLS / f"{name}.md").read_text(encoding="utf-8")


def test_size_gap_skill_uses_main_size_rule():
    """断码必须按主销码判定，且要点明"长尾码缺货不算断码"。"""
    text = _text("size-gap-diagnosis")
    assert "主销码" in text and "15%" in text
    assert "不算断码" in text
    assert "get_style_metrics" in text and "get_size_curve" in text
    # 旧的"任一码可用为 0 就算断码"的写法不能再作为结论出现
    assert "某尺码可用库存 = 0 且同款其他尺码有动销 → 断码" not in text


def test_screening_skill_labels_legacy_rule_as_prefilter_only():
    """初筛口径（零库存码数 ≥2）保留，但必须明确标注"仅候选初筛"。"""
    text = _text("sku-screening")
    assert "零库存尺码数 ≥ 2" in text
    assert "初筛" in text and "主销码" in text
    # 同一段里必须给出"对外结论走主销码口径"的指向
    assert "size-gap-diagnosis" in text


def test_restock_skill_matches_replenishment_code():
    """补货公式必须与 `skills/replenishment.py` 一致（否则模型算出来的数和工具对不上）。"""
    text = _text("restock-calc")
    for piece in ("0.3", "交期 + 3", "安全库存", "交期 + 21", "MOQ", "500"):
        assert piece in text, piece
    assert "再订货点" in text
    # 幂等键归系统（复审 A2），别让模型自己编
    assert "不要自己传幂等键" in text or "不要自己编幂等键" in text


def test_overstock_skill_states_clearance_rules():
    """清仓必须写折扣（≥6 折）与分批放量（40%），与提示词/校验器一致。"""
    text = _text("overstock-review")
    assert "6 折" in text and "40%" in text


def test_weekly_review_skill_only_saves_on_request():
    """沉淀档案只在用户要求时做（与 `chat_system.md` 第 11 条一致）。"""
    text = _text("weekly-ops-review")
    assert "save_analysis" in text
    assert "要求" in text


def test_skill_tool_mentions_exist():
    """skills 里提到的工具名必须真实存在（写错就是"派对了也答不了"）。"""
    import re

    token_re = re.compile(r"[a-z][a-z0-9_]{3,}")
    for path in sorted(SKILLS.glob("*.md")):
        tokens = set(token_re.findall(path.read_text(encoding="utf-8")))
        suspicious = {t for t in tokens
                      if t.startswith(("get_", "create_", "list_", "search_", "load_", "save_"))
                      and t not in ALL_TOOL_NAMES}
        assert not suspicious, f"{path.name} 里疑似拼错的工具名：{sorted(suspicious)}"


def test_all_skills_are_listed_by_workspace():
    """技能目录与 `workspace.SKILL_NAMES` 不能漂移（否则模型看得见文件却 load 不到）。"""
    from app.memory.workspace import SKILL_NAMES

    on_disk = {path.stem for path in SKILLS.glob("*.md")}
    assert on_disk == set(SKILL_NAMES), (sorted(on_disk), sorted(SKILL_NAMES))
