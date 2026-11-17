"""LLM 客户端（LangChain `ChatOpenAI` + DeepSeek OpenAI 兼容协议）。

要点：
- 默认模型 `deepseek-flash` 为推理模型，请求体带 `thinking: {"type": "disabled"}`
  关闭思维链（实测单次调用 15s → 7.7s），JSON 模式下稳定返回 content；
- `invoke_json` 做 Pydantic 校验 + 失败自动修复重试（最多 llm_max_retries 次）；
- 统计调用次数/Token 用量，供看板展示。
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import re
import time
from typing import Type, TypeVar

from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ValidationError

from app.config import Settings, get_settings
from app.observability import latency
from app.resilience import backoff_seconds, is_retryable, retry_after_seconds

T = TypeVar("T", bound=BaseModel)

_JSON_BLOCK = re.compile(r"\{.*\}", re.S)

# LLM 调用按部门归因（管理端卡片展示分部门调用量/token）
_department_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "llm_department", default=None
)


@contextlib.contextmanager
def department_context(department: str | None):
    token = _department_ctx.set(department)
    try:
        yield
    finally:
        _department_ctx.reset(token)


def current_department() -> str | None:
    return _department_ctx.get()


# 单轮对话的 token 用量归集（看板在回答下方展示「本轮成本」，与全局累计口径并存）
_turn_usage_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "llm_turn_usage", default=None
)


@contextlib.contextmanager
def turn_usage_context():
    state = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "errors": 0}
    token = _turn_usage_ctx.set(state)
    try:
        yield state
    finally:
        _turn_usage_ctx.reset(token)


def add_turn_usage(input_tokens: int, output_tokens: int) -> None:
    state = _turn_usage_ctx.get()
    if state is None:
        return
    state["calls"] += 1
    state["input_tokens"] += int(input_tokens or 0)
    state["output_tokens"] += int(output_tokens or 0)


def add_turn_error() -> None:
    state = _turn_usage_ctx.get()
    if state is not None:
        state["errors"] += 1


def turn_usage_snapshot() -> dict:
    state = _turn_usage_ctx.get()
    if state is None:
        return {"calls": 0, "input_tokens": 0, "output_tokens": 0, "errors": 0}
    return dict(state)


class LLMError(RuntimeError):
    pass


def cache_hit_tokens(result) -> int:  # noqa: ANN001 —— 兼容 ChatOpenAI 的 AIMessage
    """从一次模型返回里取「上下文缓存命中的 token 数」。

    为什么单独抽出来：DeepSeek 走自动前缀缓存（命中部分按更低单价计费），
    OpenAI 兼容层把它放在 `usage.prompt_cache_hit_tokens`；langchain 新版则映射到
    `usage_metadata.input_token_details.cache_read`。两处都读一遍，拿不到就记 0，
    不臆造数字（成本口径要能对上账单）。
    """
    usage = getattr(result, "usage_metadata", None) or {}
    details = usage.get("input_token_details") or {}
    hit = int(details.get("cache_read") or 0)
    if hit:
        return hit
    meta = getattr(result, "response_metadata", None) or {}
    token_usage = meta.get("token_usage") or {}
    for key in ("prompt_cache_hit_tokens", "cached_tokens", "cache_read_input_tokens"):
        value = token_usage.get(key) or meta.get(key)
        if value:
            return int(value)
    return 0


class LLMClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._semaphore = asyncio.Semaphore(self._settings.llm_max_concurrency)
        self._llm: ChatOpenAI | None = None
        # 当前正在跑的上游模型调用数：/api/health.limits 用它给出「真实并发」，
        # 而不是只报对话闸门那一半（对话侧走自己的 ChatOpenAI，不经过这里）
        self.inflight = 0
        self.stats = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "errors": 0,
                      "cache_hit_tokens": 0}
        # 分部门归因（管理员卡片可查看各部门调用/token）
        self.department_stats: dict[str, dict] = {}
        # 分来源归因：对话（ReAct 直连 ChatOpenAI）/ 巡检研判 / 其它
        self.source_stats: dict[str, dict] = {}

    def record(
        self,
        calls: int = 1,
        input_tokens: int = 0,
        output_tokens: int = 0,
        department: str | None = None,
        source: str = "other",
    ) -> None:
        """登记一次（或一批）LLM 用量：全局 + 部门 + 来源三处一起累加。

        对话 Agent 用的是自己 new 的 ChatOpenAI，不经过 `_ainvoke`，
        因此由 `chat_agent.run_chat` 汇总 usage 后调用这里补记。
        """
        calls = int(calls or 0)
        in_tokens = int(input_tokens or 0)
        out_tokens = int(output_tokens or 0)
        if calls <= 0 and in_tokens <= 0 and out_tokens <= 0:
            return
        self.stats["calls"] += calls
        self.stats["input_tokens"] += in_tokens
        self.stats["output_tokens"] += out_tokens
        if department:
            entry = self.department_stats.setdefault(
                department, {"calls": 0, "input_tokens": 0, "output_tokens": 0}
            )
            entry["calls"] += calls
            entry["input_tokens"] += in_tokens
            entry["output_tokens"] += out_tokens
        src = self.source_stats.setdefault(
            source, {"calls": 0, "input_tokens": 0, "output_tokens": 0}
        )
        src["calls"] += calls
        src["input_tokens"] += in_tokens
        src["output_tokens"] += out_tokens

    # ------------------------------------------------------------ 成本估算
    def _price(self) -> tuple[float, float]:
        s = self._settings
        return float(getattr(s, "llm_price_input", 0.0) or 0.0), float(
            getattr(s, "llm_price_output", 0.0) or 0.0
        )

    def _cost_of(self, input_tokens: int, output_tokens: int) -> float:
        price_in, price_out = self._price()
        return round(
            (int(input_tokens or 0) / 1_000_000) * price_in
            + (int(output_tokens or 0) / 1_000_000) * price_out,
            6,
        )

    def cost(self) -> dict:
        """按 token 单价估算成本（元）：全局 + 分来源 + 分部门。

        单价来自 `LLM_PRICE_INPUT/LLM_PRICE_OUTPUT`（元/百万 tokens），默认值只作量级参考；
        真实报价请以合同/账单为准，这里只回答"哪条链路在烧钱"。
        """
        price_in, price_out = self._price()
        sources = {
            name: {
                **dict(v),
                "cost_cny": self._cost_of(v.get("input_tokens", 0), v.get("output_tokens", 0)),
            }
            for name, v in (self.source_stats or {}).items()
        }
        departments = {
            name: {
                **dict(v),
                "cost_cny": self._cost_of(v.get("input_tokens", 0), v.get("output_tokens", 0)),
            }
            for name, v in (self.department_stats or {}).items()
        }
        return {
            "total_cny": self._cost_of(
                self.stats.get("input_tokens", 0), self.stats.get("output_tokens", 0)
            ),
            "price_input_per_million": price_in,
            "price_output_per_million": price_out,
            "currency": "CNY",
            # 上下文缓存命中情况：命中部分按更低单价计费，命中率越高越省钱
            "prompt_cache": {
                "hit_tokens": int(self.stats.get("cache_hit_tokens", 0) or 0),
                "input_tokens": int(self.stats.get("input_tokens", 0) or 0),
                "hit_rate": round(
                    int(self.stats.get("cache_hit_tokens", 0) or 0)
                    / int(self.stats.get("input_tokens", 1) or 1),
                    4,
                ),
            },
            "sources": sources,
            "departments": departments,
            "note": "按 LLM_PRICE_INPUT/OUTPUT（元/百万 tokens）估算，可在 .env 按合同价覆盖",
        }

    # ------------------------------------------------------------ 底层
    def _get_llm(self) -> ChatOpenAI:
        if self._llm is None:
            if not self._settings.deepseek_api_key:
                raise LLMError("未配置 DEEPSEEK_API_KEY（检查项目根目录 .env）")
            self._llm = ChatOpenAI(
                model=self._settings.llm_model,
                api_key=self._settings.deepseek_api_key,
                base_url=self._settings.deepseek_base_url,
                temperature=self._settings.llm_temperature,
                max_tokens=self._settings.llm_max_tokens,
                timeout=self._settings.llm_timeout_seconds,
                max_retries=0,
            )
        return self._llm

    def _bind_request(self, llm: ChatOpenAI, *, json_mode: bool = True) -> ChatOpenAI:
        payload: dict = {}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if self._settings.llm_disable_thinking:
            payload["extra_body"] = {"thinking": {"type": "disabled"}}
        return llm.bind(**payload) if payload else llm

    async def _ainvoke(self, messages: list[dict], max_tokens: int | None = None,
                       *, json_mode: bool = True):
        """调用模型：**瞬时失败（网络/限流/超时）按 LLM_MAX_RETRIES 重试**。

        早先这里是「一次调用、失败即抛」：`LLM_MAX_RETRIES` 名字像网络重试，实际只用于
        `invoke_json` 的 JSON 修复重试，于是巡检/研判链路上一次网络抖动就整条挂掉。
        现在：传输层重试走 `is_retryable`，JSON 修复重试走 `llm_json_repair_retries`。
        """
        llm = self._get_llm()
        if max_tokens:
            llm = llm.bind(max_tokens=max_tokens)
        chain = self._bind_request(llm, json_mode=json_mode)
        retries = max(0, int(getattr(self._settings, "llm_max_retries", 0)))
        last_exc: BaseException | None = None
        for attempt in range(retries + 1):
            started = time.perf_counter()
            try:
                async with self._semaphore:
                    self.inflight += 1
                    try:
                        result = await chain.ainvoke(messages)
                    finally:
                        self.inflight -= 1
            except Exception as exc:  # noqa: BLE001 —— 只重试瞬时错误
                last_exc = exc
                if attempt >= retries or not is_retryable(exc):
                    raise
                wait = retry_after_seconds(exc)
                await asyncio.sleep(wait if wait is not None else backoff_seconds(attempt))
                continue
            finally:
                latency.record("llm", (time.perf_counter() - started) * 1000)
            return self._collect_usage(result)
        raise last_exc if last_exc is not None else LLMError("LLM 调用失败")

    def _collect_usage(self, result) -> str:  # noqa: ANN001 —— ChatOpenAI 的 AIMessage
        """记账 + 取出文本内容（token / 缓存命中 / 部门 / 来源）。"""
        usage = getattr(result, "usage_metadata", None) or {}
        in_tokens = int(usage.get("input_tokens") or 0)
        out_tokens = int(usage.get("output_tokens") or 0)
        hit_tokens = cache_hit_tokens(result)
        add_turn_usage(in_tokens, out_tokens)
        self.record(calls=1, input_tokens=in_tokens, output_tokens=out_tokens,
                    department=current_department(), source="other")
        if hit_tokens:
            self.stats["cache_hit_tokens"] = int(self.stats.get("cache_hit_tokens", 0)) + hit_tokens
        content = result.content
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            )
        return str(content or "")

    # ------------------------------------------------------------ 对外
    async def invoke_text(self, system: str, user: str, max_tokens: int | None = None,
                          *, json_mode: bool = False) -> str:
        """自由文本调用（默认不要求 JSON）。

        为什么默认关掉 json_mode：DeepSeek 的 `response_format=json_object` 要求提示里
        出现 "json" 字样，否则直接 400——溯源重写、段落润色这类场景要的是纯文本。
        """
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        return await self._ainvoke(messages, max_tokens=max_tokens, json_mode=json_mode)

    async def invoke_json(
        self,
        system: str,
        user: str,
        schema: Type[T],
        max_tokens: int | None = None,
    ) -> T:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        last_error: Exception | None = None
        repair_retries = max(0, int(getattr(self._settings, "llm_json_repair_retries", 2)))
        for _attempt in range(repair_retries + 1):
            raw = await self._ainvoke(messages, max_tokens=max_tokens)
            try:
                return self._parse(raw, schema)
            except (ValidationError, ValueError) as exc:
                self.stats["errors"] += 1
                add_turn_error()
                last_error = exc
                messages = messages + [
                    {"role": "assistant", "content": raw[:1200]},
                    {
                        "role": "user",
                        "content": (
                            "上一条回复不是符合要求的 JSON。错误：" + str(exc)[:400]
                            + "\n请只输出一个合法 JSON 对象，字段与要求一致，不要输出解释。"
                        ),
                    },
                ]
        raise LLMError(f"LLM JSON 输出校验失败（已重试）: {last_error}")

    @staticmethod
    def _parse(raw: str, schema: Type[T]) -> T:
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            m = _JSON_BLOCK.search(raw)
            if not m:
                raise ValueError("回复中未找到 JSON 对象") from None
            data = json.loads(m.group(0))
        return schema.model_validate(data)


_llm: LLMClient | None = None


def get_llm() -> LLMClient:
    global _llm
    if _llm is None:
        _llm = LLMClient()
    return _llm


def record_usage(calls: int = 1, input_tokens: int = 0, output_tokens: int = 0,
                 department: str | None = None, source: str = "other") -> None:
    """外部模块（如对话 Agent 的 ChatOpenAI）补登记 LLM 用量，供看板统一统计。"""
    get_llm().record(calls=calls, input_tokens=input_tokens, output_tokens=output_tokens,
                     department=department, source=source)


def reset_llm() -> None:
    global _llm
    _llm = None
