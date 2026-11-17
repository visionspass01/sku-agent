"""校验规则：用户参数错必须抛错，链路降级只能记 warning。"""

from __future__ import annotations

import pytest

from app.agents_image.config import ImageSettings
from app.agents_image.nodes import validate as v
from app.agents_image.nodes.plan_slots import compose
from app.agents_image.nodes.understand import understand
from app.agents_image.registry import get_registry, reset_registry
from app.agents_image.state import ImageTaskState, RefImage
from app.core.errors import ImageError


def _state(**overrides) -> ImageTaskState:
    base = {"prompt": "白底主图，正面全身像上装突出", "aspect": "1:1", "count": 1,
            "refs": [RefImage(asset_id="a" * 8)], "scene_styles": ["影棚正式风"]}
    base.update(overrides)
    return ImageTaskState(**base)


def _composed(**overrides) -> ImageTaskState:
    """走一遍 understand + compose，得到「真实形态」的 state（含 prompt_pack/preserve）。"""
    state = _state(**overrides)
    state.attrs = understand(state)
    state.prompt_pack = compose(state, get_registry())
    return state


@pytest.fixture(autouse=True)
def _fresh_registry():
    reset_registry()
    yield
    reset_registry()


def test_valid_state_passes():
    state = _composed()
    v.validate(state, settings=ImageSettings())      # 不抛即通过
    assert state.attrs["must_not_change"]
    assert all(d.preserve for d in state.prompt_pack)


def test_reference_required():
    with pytest.raises(ImageError) as exc:
        v.validate(_state(refs=[]))
    assert exc.value.code == "IMG_REFERENCE_REQUIRED"


def test_too_many_references():
    refs = [RefImage(asset_id=str(i) * 8) for i in range(7)]
    with pytest.raises(ImageError) as exc:
        v.validate(_state(refs=refs), settings=ImageSettings())
    assert exc.value.code == "IMG_TOO_MANY_REFS"


@pytest.mark.parametrize("aspect", ["2:1", "9:16", "auto", ""])
def test_bad_aspect(aspect):
    with pytest.raises(ImageError) as exc:
        v.validate(_state(aspect=aspect))
    assert exc.value.code == "IMG_INPUT_INVALID"


@pytest.mark.parametrize("count", [0, 10, -1])
def test_bad_count(count):
    with pytest.raises(ImageError) as exc:
        v.validate(_state(count=count), settings=ImageSettings())
    assert exc.value.code == "IMG_INPUT_INVALID"


def test_count_limited_by_settings():
    with pytest.raises(ImageError) as exc:
        v.validate(_state(count=4), settings=ImageSettings(max_total_images=3))
    assert exc.value.code == "IMG_INPUT_INVALID"


def test_empty_prompt_rejected():
    with pytest.raises(ImageError) as exc:
        v.validate(_state(prompt="   "))
    assert exc.value.code == "IMG_INPUT_INVALID"


@pytest.mark.parametrize("word", ["伪造", "官方认证", "真人明星", "冒充实拍", "绝对化"])
def test_forbidden_words_in_request(word):
    with pytest.raises(ImageError) as exc:
        v.validate(_state(prompt=f"白底主图 {word}"))
    assert exc.value.code == "IMG_CONTENT_BLOCKED"
    assert word in exc.value.detail


def test_forbidden_words_in_prompt_pack():
    state = _composed()
    state.prompt_pack[0].prompt += "（官方认证）"
    with pytest.raises(ImageError) as exc:
        v.validate(state, settings=ImageSettings())
    assert exc.value.code == "IMG_CONTENT_BLOCKED"


def test_platform_extra_forbidden_words():
    state = _state(prompt="白底主图 全网最低价促销")
    v.validate(state)                                    # 通用规则不管「最低价」
    with pytest.raises(ImageError) as exc:
        v.validate(state, extra_forbidden=("全网最低价",))
    assert exc.value.code == "IMG_CONTENT_BLOCKED"


def test_prompt_pack_must_keep_identity_lock():
    state = _composed()
    state.prompt_pack[0].preserve = ["颜色"]              # 丢掉「版型/图案」等锁定项
    with pytest.raises(ImageError) as exc:
        v.validate(state, settings=ImageSettings())
    assert exc.value.code == "IMG_SCHEMA_INVALID"
    assert "版型" in exc.value.detail


def test_prompt_pack_empty_prompt_rejected():
    state = _composed()
    state.prompt_pack[0].prompt = "  "
    with pytest.raises(ImageError) as exc:
        v.validate(state, settings=ImageSettings())
    assert exc.value.code == "IMG_SCHEMA_INVALID"


def test_prompt_pack_aspect_must_match_request():
    state = _composed()
    state.prompt_pack[0].aspect = "16:9"
    with pytest.raises(ImageError) as exc:
        v.validate(state, settings=ImageSettings())
    assert exc.value.code == "IMG_SCHEMA_INVALID"


def test_validate_fills_attrs_when_missing():
    state = _state()
    state.attrs = {}
    v.validate(state)
    assert state.attrs["must_not_change"]                 # 校验后 attrs 已被规整成完整 schema


def test_degrade_note_is_warning_only_and_idempotent():
    state = _composed()
    state.mcp_degraded = True
    v.degrade_note(state)
    v.degrade_note(state)
    notes = [w for w in state.warnings if w.startswith("IMG_MCP_DEGRADED")]
    assert len(notes) == 1
    v.validate(state, settings=ImageSettings())            # 降级不阻塞出图

    clean = _composed()
    v.degrade_note(clean)
    assert clean.warnings == []


def test_understand_degrades_bad_model_output_into_warnings():
    state = _state()
    attrs = understand(state, raw="{不是 json")
    assert attrs["source"] == "heuristic"
    assert any("IMG_SCHEMA_INVALID" in w for w in state.warnings)
    assert attrs["must_not_change"]                       # 降级后身份锁定仍在
