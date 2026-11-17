"""结构化 schema：服装属性 / 生图意图的默认值、边界与「解析失败只降级」的语义。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agents_image.schemas import (
    DEFAULT_MUST_NOT_CHANGE,
    ClothingAttrs,
    ImageIntent,
    attrs_of,
    merge_preserve,
    parse_attrs,
    parse_intent,
)


def test_attrs_defaults_keep_identity_lock_non_empty():
    attrs = ClothingAttrs()
    assert attrs.product_type == "服装商品"
    assert attrs.color_hex == "#FFFFFF"
    assert attrs.category == "通用" and attrs.source == "heuristic"
    assert attrs.must_not_change == DEFAULT_MUST_NOT_CHANGE      # 身份锁定必须有值
    assert attrs.confidence == 0.0


def test_attrs_prompt_block_contains_locked_facts():
    attrs = ClothingAttrs(product_type="针织开衫", category="女装", shape="落肩直筒",
                          color_names=["米白"], materials="羊毛混纺", logo_text="无")
    block = attrs.prompt_block()
    assert "针织开衫" in block and "女装" in block and "米白" in block
    assert "羊毛混纺" in block


@pytest.mark.parametrize("bad", ["红色", "#FFF", "#12345G", "", "FFFFFF"])
def test_attrs_reject_bad_hex(bad):
    with pytest.raises(ValidationError):
        ClothingAttrs(color_hex=bad)


def test_attrs_dedupe_and_restore_must_not_change():
    attrs = ClothingAttrs(must_not_change=["颜色", " 颜色 ", "", "版型"])
    assert attrs.must_not_change == ["颜色", "版型"]
    blank = ClothingAttrs(must_not_change=[])
    assert blank.must_not_change == DEFAULT_MUST_NOT_CHANGE      # 不许清空


def test_attrs_clean_key_features():
    attrs = ClothingAttrs(key_features=["  领口螺纹  ", "领口螺纹", "x" * 200, ""])
    assert attrs.key_features[0] == "领口螺纹" and attrs.key_features.count("领口螺纹") == 1
    assert len(attrs.key_features[1]) <= 40
    many = ClothingAttrs(key_features=[f"特征{i}" for i in range(20)])
    assert len(many.key_features) == 8


def test_attrs_ignores_extra_fields_from_model():
    attrs = ClothingAttrs(product_type="卫衣", fabric_grade="A", 颜色="黑")
    assert attrs.product_type == "卫衣"
    assert not hasattr(attrs, "fabric_grade")


def test_parse_attrs_accepts_fenced_json_and_merges_fallback():
    raw = """```json
    {"product_type": "牛仔外套", "color_hex": "#2C5D8F", "must_not_change": ["颜色", "版型"]}
    ```"""
    attrs, warnings = parse_attrs(raw, fallback={"shape": "宽松版型", "source": "heuristic"})
    assert warnings == []
    assert attrs.product_type == "牛仔外套"
    assert attrs.shape == "宽松版型"           # 模型没给的字段用兜底值
    assert attrs.must_not_change == ["颜色", "版型"]


@pytest.mark.parametrize("raw", ["不是 json", "{broken", "[]", "null"])
def test_parse_attrs_degrades_on_bad_json(raw):
    attrs, warnings = parse_attrs(raw, fallback={"product_type": "毛衣"})
    assert attrs.product_type == "毛衣" and attrs.source == "heuristic"
    assert len(warnings) == 1 and warnings[0].startswith("IMG_SCHEMA_INVALID")


def test_parse_attrs_degrades_on_schema_violation():
    attrs, warnings = parse_attrs({"color_hex": "红色", "confidence": 3},
                                 fallback={"product_type": "衬衫"})
    assert attrs.product_type == "衬衫"
    assert warnings and "IMG_SCHEMA_INVALID" in warnings[0]


def test_parse_attrs_without_raw_uses_fallback_only():
    attrs, warnings = parse_attrs(None, fallback={"product_type": "半身裙", "confidence": 0.3})
    assert attrs.product_type == "半身裙" and attrs.confidence == 0.3
    assert warnings == []
    blank, blank_warnings = parse_attrs("   ", fallback={"product_type": "半身裙"})
    assert blank.product_type == "半身裙" and blank_warnings == []   # 空输出 = 正常形态，不算降级


def test_attrs_of_normalises_inputs():
    assert attrs_of(None).product_type == "服装商品"
    assert attrs_of({"product_type": "马甲"}).product_type == "马甲"
    assert attrs_of(ClothingAttrs(product_type="夹克")).product_type == "夹克"


def test_intent_defaults_and_bounds():
    intent = ImageIntent()
    assert intent.goal == "main" and intent.aspect == "1:1" and intent.count == 1
    assert ImageIntent(count=9).count == 9
    for bad in ({"count": 0}, {"count": 10}, {"aspect": "2:1"}, {"goal": "banner"}):
        with pytest.raises(ValidationError):
            ImageIntent(**bad)


def test_intent_dedupes_lists():
    intent = ImageIntent(compositions=["正面全身像", "正面全身像 ", ""],
                         scene_styles=["影棚正式风"], constraints=["不得改色"])
    assert intent.compositions == ["正面全身像"]
    assert intent.scene_styles == ["影棚正式风"] and intent.constraints == ["不得改色"]


def test_parse_intent_degrades_to_request_params():
    intent, warnings = parse_intent('{"count": "很多"}', fallback={"count": 2, "aspect": "3:4"})
    assert intent.count == 2 and intent.aspect == "3:4"
    assert warnings and "IMG_SCHEMA_INVALID" in warnings[0]

    ok, no_warnings = parse_intent({"count": 3, "scene_styles": ["街拍清新风"]})
    assert ok.count == 3 and no_warnings == []


def test_merge_preserve_is_superset_of_identity_lock():
    merged = merge_preserve(["领口"], ["颜色", "版型", "领口", " "])
    assert merged == ["领口", "颜色", "版型"]
    assert set(DEFAULT_MUST_NOT_CHANGE).issubset(merge_preserve(None, DEFAULT_MUST_NOT_CHANGE))
    assert merge_preserve(None, None) == []
