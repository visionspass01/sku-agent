"""LLM / 视觉解析服装属性（第五轮 4.4）：模型输出 → `parse_attrs` 收口。

设计（与简历职责 3 对齐）：
- 开关默认全关：`IMAGE_LLM_PARSE_ENABLED`（文本解析）/ `IMAGE_VISION_ENABLED`（视觉解析），
  没有 Key 或没装依赖时**自动降级**为启发式，并在 `warnings` 留 `IMG_LLM_PARSE_DEGRADED` /
  `IMG_VISION_DISABLED`，不阻塞出图；
- 模型输出**不直接进 Prompt**：一律交给 `schemas.parse_attrs(raw, fallback=heuristic)`
  （容忍 ```json 围栏/前后解释文字，schema 不合规就退回启发式 + `IMG_SCHEMA_INVALID`），
  这就是「结构化输出 + Schema 收口」的落点；
- 用量记进 `metrics.record_llm()`，供 `/api/image/metrics` 与五张卡展示 token/成本。
"""

from __future__ import annotations

import base64
import logging

from app.agents_image import metrics as image_metrics
from app.agents_image.nodes.understand import heuristic_attrs
from app.agents_image.schemas import parse_attrs

logger = logging.getLogger("sku-agent.image.llm_parse")

PARSE_SYSTEM = (
    "你是电商服装图片的结构化解析器。只输出一个 JSON 对象，字段："
    "product_type(品类+形态)、category(男装/女装/童装/通用)、shape(版型描述)、"
    "color_hex(主色 #RRGGBB)、color_names(颜色名数组)、logo_text(图上文字，无则 无)、"
    "materials(材质与工艺)、key_features(关键特征数组，≤8)、"
    "must_not_change(绝不能变的要素数组)、confidence(0-1)。"
    "信息不足的字段用保守默认值，不要编造细节。"
)


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


def _raw_to_attrs(state, raw: str, *, source: str = "llm") -> tuple[dict | None, list[str]]:
    """模型输出 → `parse_attrs`（Schema 收口）；解析失败退回启发式并留 warning。"""
    fallback = heuristic_attrs(f"{state.prompt} {' '.join(state.scene_styles or [])}")
    parsed = raw
    try:  # 标记来源：payload 里显式带 source，ClothingAttrs 的枚举里有 llm/vision
        from app.agents_image.schemas import _extract_json

        payload = _extract_json(raw)
        parsed = {**payload, "source": source}
    except Exception:  # noqa: BLE001 —— 捞不出 JSON 就原样交给 parse_attrs（它会降级）
        parsed = raw
    attrs, warnings = parse_attrs(parsed, fallback=fallback)
    return attrs.model_dump(), warnings


async def parse_text(state, settings) -> tuple[dict | None, list[str]]:
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
    attrs, warnings = _raw_to_attrs(state, raw)
    return attrs, warnings


async def parse_with_vision(state, settings) -> tuple[dict | None, list[str]]:
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

        from app.agents_image.storage.image_storage import get_storage
    except Exception as exc:  # noqa: BLE001
        return None, [f"IMG_VISION_DEGRADED: 依赖不可用（{type(exc).__name__}）"]

    storage = get_storage()
    content: list[dict] = [{"type": "text", "text": _user_prompt(state)}]
    for ref in refs[:2]:
        try:
            data = storage.get(str(getattr(ref, "key", "") or ""))
        except Exception:  # noqa: BLE001 —— 文件缺失就跳过这张图
            continue
        encoded = base64.b64encode(data).decode("ascii")
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{encoded}"}})
    if len(content) == 1:
        return None, ["IMG_VISION_DEGRADED: 参考图字节读不出来，图片理解已跳过"]

    payload = {"model": model, "messages": [
        {"role": "system", "content": PARSE_SYSTEM},
        {"role": "user", "content": content},
    ], "temperature": 0.1}
    base = str(getattr(settings, "ark_base_url", "") or "").rstrip("/")
    before = _llm_usage_snapshot()
    try:
        async with httpx.AsyncClient(timeout=float(getattr(settings, "mcp_timeout_seconds", 5.0))) as client:
            resp = await client.post(f"{base}/chat/completions", json=payload,
                                     headers={"Authorization": f"Bearer {api_key}"})
            resp.raise_for_status()
            raw = resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001 —— 视觉解析失败退回启发式
        logger.warning("视觉解析失败，退回启发式：%s", exc)
        return None, [f"IMG_VISION_DEGRADED: 视觉解析失败（{type(exc).__name__}），已退回启发式"]
    finally:
        _record_usage(before)
    attrs, warnings = _raw_to_attrs(state, raw, source="vision")
    return attrs, warnings


async def parse_clothing_attrs(state, settings) -> tuple[dict | None, list[str]]:
    """视觉优先、其次文本解析；都不可用返回 (None, []) 由调用方退回启发式。"""
    attrs, warnings = await parse_with_vision(state, settings)
    if attrs is not None:
        return attrs, warnings
    text_attrs, text_warnings = await parse_text(state, settings)
    return text_attrs, warnings + text_warnings


__all__ = ["parse_clothing_attrs", "parse_text", "parse_with_vision", "PARSE_SYSTEM"]
