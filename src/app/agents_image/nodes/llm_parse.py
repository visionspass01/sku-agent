"""LLM / 视觉解析服装属性（第五轮 4.4，第六轮 P1-2/P1-3 修复）。

设计：
- 开关默认全关：`IMAGE_LLM_PARSE_ENABLED`（文本解析）/ `IMAGE_VISION_ENABLED`（视觉解析），
  触发策略由 `IMAGE_LLM_PARSE_MODE` 决定：
  - `off`：从不解析；
  - `fallback`（默认）：**只有中台字段不全时**才解析（中台为准，模型补缺）；
  - `always`：总是解析一遍，但仍然只补空字段（不覆盖中台权威字段）；
- 模型输出**不直接进 Prompt**：一律交给 `schemas.parse_attrs(raw, fallback=...)`
  （容忍 ```json 围栏/前后解释文字，schema 不合规就退回 fallback + `IMG_SCHEMA_INVALID`）；
- **参考图字节按 `asset_id → ImageAsset.key` 读取**（第六轮 P1-2：过去取 `ref.key`，
  `RefImage` 根本没有这个字段 → 视觉路径 100% 降级）；
- 用量记进 `metrics.record_llm()`，供 `/api/image/metrics` 与五张卡展示 token/成本。
"""

from __future__ import annotations

import base64
import logging
import re

from app.agents_image import metrics as image_metrics
from app.agents_image.nodes.understand import heuristic_attrs
from app.agents_image.schemas import _extract_json, parse_attrs

logger = logging.getLogger("sku-agent.image.llm_parse")

PARSE_SYSTEM = (
    "你是电商服装图片的结构化解析器。只输出一个 JSON 对象，字段："
    "product_type(品类+形态)、category(男装/女装/童装/通用)、shape(版型描述)、"
    "color_hex(主色 #RRGGBB)、color_names(颜色名数组)、logo_text(图上文字，无则 无)、"
    "materials(材质与工艺)、key_features(关键特征数组，≤8)、"
    "must_not_change(绝不能变的要素数组)、confidence(0-1)。"
    "信息不足的字段用保守默认值，不要编造细节。"
)

PARSE_MODES = ("off", "fallback", "always")
_HEX_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")
_PLACEHOLDERS = {"", "服装商品", "以参考图为准", "无", "通用"}


def attrs_incomplete(attrs: dict | None) -> bool:
    """判断中台给的属性是否"缺得值得让模型补"（第六轮 P1-3 的触发条件）。"""
    data = dict(attrs or {})
    if not data:
        return True
    color_hex = str(data.get("color_hex") or "").strip()
    if not _HEX_PATTERN.match(color_hex):
        return True
    for key in ("product_type", "shape", "materials"):
        if str(data.get(key) or "").strip() in _PLACEHOLDERS:
            return True
    return False


def merge_missing_attrs(base: dict | None, parsed: dict | None) -> dict:
    """中台字段优先、模型只补空位（第六轮 P1-3）。

    为什么不能直接 `{**base, **parsed}`：中台的 `attrs` 是权威数据（颜色 hex、版型、
    must_not_change 都来自中台），让模型覆盖它等于把"身份锁定"交给模型猜。
    """
    merged = dict(base or {})
    for key, value in (parsed or {}).items():
        if key in {"source", "must_not_change"}:
            continue
        current = merged.get(key)
        if current in (None, "", [], {}) or str(current).strip() in _PLACEHOLDERS:
            merged[key] = value
    union: list[str] = []
    for item in list((base or {}).get("must_not_change") or []) + \
            list((parsed or {}).get("must_not_change") or []):
        text = str(item or "").strip()
        if text and text not in union:
            union.append(text)
    merged["must_not_change"] = union or ["颜色", "版型", "图案", "logo 文字", "关键部件位置"]
    merged["source"] = str((parsed or {}).get("source") or merged.get("source") or "heuristic")
    return merged


def _user_prompt(state) -> str:
    refs = getattr(state, "refs", None) or []
    return (
        f"需求描述：{state.prompt}\n"
        f"平台：{state.platform}\n"
        f"构图：{'、'.join(state.compositions or []) or '默认'}\n"
        f"场景风格：{'、'.join(state.scene_styles or []) or '默认'}\n"
        f"参考图数量：{len(refs)}\n"
        "请给出该服装商品的身份锁定字段 JSON。"
    )


def _llm_usage_snapshot() -> dict:
    try:
        from app.llm import get_llm

        stats = dict(getattr(get_llm(), "stats", {}) or {})
        return {"calls": int(stats.get("calls") or 0),
                "input_tokens": int(stats.get("input_tokens") or 0),
                "output_tokens": int(stats.get("output_tokens") or 0)}
    except Exception:  # noqa: BLE001
        return {"calls": 0, "input_tokens": 0, "output_tokens": 0}


def _record_usage(before: dict) -> None:
    after = _llm_usage_snapshot()
    calls = max(0, after["calls"] - before["calls"])
    tokens = max(0, after["input_tokens"] - before["input_tokens"]
                 + after["output_tokens"] - before["output_tokens"])
    if calls or tokens:
        image_metrics.record_llm(calls=calls, tokens=tokens)


def _fallback_of(state, base_attrs: dict | None) -> dict:
    if base_attrs:
        return dict(base_attrs)
    return heuristic_attrs(f"{state.prompt} {' '.join(state.scene_styles or [])}")


def _raw_to_attrs(state, raw: str, *, source: str = "llm",
                  base_attrs: dict | None = None) -> tuple[dict | None, list[str]]:
    """模型输出 → `parse_attrs`（Schema 收口）；解析失败退回 fallback 并留 warning。"""
    fallback = _fallback_of(state, base_attrs)
    parsed = raw
    try:  # 标记来源：payload 里显式带 source（ClothingAttrs 枚举含 llm/vision）
        payload = _extract_json(raw)
        parsed = {**payload, "source": source}
    except Exception:  # noqa: BLE001 —— 捞不出 JSON 就原样交给 parse_attrs（它会降级）
        parsed = raw
    attrs, warnings = parse_attrs(parsed, fallback=fallback)
    return attrs.model_dump(), warnings


async def parse_text(state, settings, *, base_attrs: dict | None = None
                     ) -> tuple[dict | None, list[str]]:
    """文本解析（JSON mode）：需求描述 + 图位信息 → 身份锁定字段。"""
    if not bool(getattr(settings, "llm_parse_enabled", False)):
        return None, []
    try:
        from app.llm import get_llm
    except Exception as exc:  # noqa: BLE001 —— 模型依赖不可用
        return None, [f"IMG_LLM_PARSE_DEGRADED: 模型不可用（{type(exc).__name__}），已退回启发式"]
    before = _llm_usage_snapshot()
    try:
        raw = await get_llm().invoke_text(PARSE_SYSTEM, _user_prompt(state), json_mode=True)
    except Exception as exc:  # noqa: BLE001 —— 无 Key/网络/限流：降级不阻断
        logger.warning("LLM 解析失败，退回启发式：%s", exc)
        return None, [f"IMG_LLM_PARSE_DEGRADED: 模型解析失败（{type(exc).__name__}），已退回启发式"]
    finally:
        _record_usage(before)
    attrs, warnings = _raw_to_attrs(state, raw, base_attrs=base_attrs)
    return attrs, warnings


def _ref_bytes(ref) -> tuple[bytes, str] | None:
    """按 `asset_id → ImageAsset.key` 读参考图字节（第六轮 P1-2 的核心修复）。

    过去这里取 `ref.key`，而 `state.refs` 里是 `RefImage`（只有 asset_id/sha256/mime…），
    于是永远读不到字节、视觉解析 100% 走降级。现在走资产表拿 key 再读存储。
    """
    asset_id = str(getattr(ref, "asset_id", "") or "")
    if not asset_id:
        return None
    try:
        from app.agents_image.storage.image_storage import get_storage
        from app.agents_image.store import assets as asset_store
        from app.db.engine import session_scope

        with session_scope() as session:
            asset = asset_store.get_asset(session, asset_id)
            if asset is None:
                return None
            key = str(getattr(asset, "key", "") or "")
            mime = str(getattr(asset, "mime", "") or "image/png")
        if not key:
            return None
        return get_storage().get(key), mime
    except Exception as exc:  # noqa: BLE001 —— 文件缺失/清理过：跳过这张图
        logger.info("参考图字节读取失败（跳过该图）：%s", exc)
        return None


async def parse_with_vision(state, settings, *, base_attrs: dict | None = None
                            ) -> tuple[dict | None, list[str]]:
    """视觉解析（Ark / OpenAI 兼容）：把参考图交给视觉模型，再走同一个 Schema 收口。"""
    if not bool(getattr(settings, "vision_enabled", False)):
        return None, []
    api_key = str(getattr(settings, "ark_api_key", "") or "").strip()
    model = str(getattr(settings, "ark_vision_model", "") or "").strip()
    if not api_key or not model:
        return None, ["IMG_VISION_DISABLED: 未配置 ARK_API_KEY / ARK_VISION_MODEL，图片理解已跳过"]
    refs = [item for item in (getattr(state, "refs", None) or [])
            if str(getattr(item, "asset_id", "") or "")]
    if not refs:
        return None, ["IMG_VISION_DEGRADED: 没有可用的参考图字节，图片理解已跳过"]
    try:
        import httpx
    except Exception as exc:  # noqa: BLE001
        return None, [f"IMG_VISION_DEGRADED: 依赖不可用（{type(exc).__name__}）"]

    content: list[dict] = [{"type": "text", "text": _user_prompt(state)}]
    for ref in refs[:2]:
        got = _ref_bytes(ref)
        if got is None:
            continue
        data, mime = got
        encoded = base64.b64encode(data).decode("ascii")
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:{mime or 'image/png'};base64,{encoded}"}})
    if len(content) == 1:
        return None, ["IMG_VISION_DEGRADED: 参考图字节读不出来，图片理解已跳过"]

    payload = {"model": model, "messages": [
        {"role": "system", "content": PARSE_SYSTEM},
        {"role": "user", "content": content},
    ], "temperature": 0.1}
    base = str(getattr(settings, "ark_base_url", "") or "").rstrip("/")
    timeout = float(getattr(settings, "vision_timeout_seconds", 15.0) or 15.0)
    before = _llm_usage_snapshot()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{base}/chat/completions", json=payload,
                                     headers={"Authorization": f"Bearer {api_key}"})
            resp.raise_for_status()
            raw = resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001 —— 视觉解析失败退回启发式
        logger.warning("视觉解析失败，退回启发式：%s", exc)
        return None, [f"IMG_VISION_DEGRADED: 视觉解析失败（{type(exc).__name__}），已退回启发式"]
    finally:
        _record_usage(before)
    attrs, warnings = _raw_to_attrs(state, raw, source="vision", base_attrs=base_attrs)
    return attrs, warnings


async def parse_clothing_attrs(state, settings, *, base_attrs: dict | None = None
                               ) -> tuple[dict | None, list[str]]:
    """视觉优先、其次文本解析；都不可用返回 (None, []) 由调用方退回启发式。"""
    attrs, warnings = await parse_with_vision(state, settings, base_attrs=base_attrs)
    if attrs is not None:
        return attrs, warnings
    text_attrs, text_warnings = await parse_text(state, settings, base_attrs=base_attrs)
    return text_attrs, warnings + text_warnings


__all__ = ["parse_clothing_attrs", "parse_text", "parse_with_vision", "PARSE_SYSTEM",
           "PARSE_MODES", "attrs_incomplete", "merge_missing_attrs", "_ref_bytes"]
