"""LLMClient 传输层重试（B2）：`LLM_MAX_RETRIES` 终于真的作用于网络错误。

回归背景：`_ainvoke` 之前是「一次调用、失败即抛」。`LLM_MAX_RETRIES` 只被
`invoke_json` 用来做 JSON 修复重试，于是巡检/研判链路上一次网络抖动整条挂掉，
而名字让人以为它已经是网络重试次数。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel


class _Reply(BaseModel):
    ok: bool


class _FailingLLM:
    """可编排的 ChatOpenAI 替身：前 N 次抛异常，之后返回固定内容。"""

    def __init__(self, failures: int, exc: BaseException, content: str = "可用库存 1 件") -> None:
        self.failures = failures
        self.exc = exc
        self.content = content
        self.calls = 0
        self.bound: list[dict] = []

    def bind(self, **kwargs):  # noqa: ANN003
        self.bound.append(dict(kwargs))
        return self

    async def ainvoke(self, messages):  # noqa: ANN001
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc
        return SimpleNamespace(
            content=self.content,
            usage_metadata={"input_tokens": 10, "output_tokens": 5},
            response_metadata={},
        )


@pytest.fixture()
def no_sleep(monkeypatch):
    slept: list[float] = []

    async def _sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("app.llm.asyncio.sleep", _sleep)
    return slept


def _client(stub, _settings, **overrides):
    from app.llm import LLMClient

    settings = _settings.model_copy(deep=True)
    settings.llm_max_retries = 2
    settings.llm_json_repair_retries = 1
    for key, value in overrides.items():
        setattr(settings, key, value)
    client = LLMClient(settings)
    client._llm = stub
    return client


def test_transient_error_is_retried_then_succeeds(_settings, no_sleep):
    stub = _FailingLLM(2, ConnectionError("connection reset"))
    client = _client(stub, _settings)

    text = __import__("asyncio").run(client.invoke_text("sys", "user"))

    assert text == "可用库存 1 件"
    assert stub.calls == 3          # 首次 + 2 次重试（= llm_max_retries）
    assert len(no_sleep) == 2


def test_non_retryable_error_is_raised_immediately(_settings, no_sleep):
    class _BadRequest(Exception):
        def __init__(self) -> None:
            super().__init__("invalid_request_error: bad parameter")
            self.status_code = 400

    stub = _FailingLLM(5, _BadRequest())
    client = _client(stub, _settings)

    with pytest.raises(_BadRequest):
        __import__("asyncio").run(client.invoke_text("sys", "user"))
    assert stub.calls == 1
    assert no_sleep == []


def test_retry_after_header_wins_over_backoff(_settings, no_sleep):
    class _Limited(Exception):
        def __init__(self) -> None:
            super().__init__("429 too many requests")
            self.status_code = 429
            self.response = SimpleNamespace(headers={"retry-after": "3"})

    stub = _FailingLLM(1, _Limited())
    client = _client(stub, _settings)

    __import__("asyncio").run(client.invoke_text("sys", "user"))
    assert no_sleep == [3.0]


def test_json_repair_retries_are_configured_separately(_settings, no_sleep):
    from app.llm import LLMError

    # 模型一直返回非法 JSON：修复重试用 llm_json_repair_retries，与网络重试互不影响
    stub = _FailingLLM(0, ConnectionError("unused"), content="这不是 JSON")
    client = _client(stub, _settings, llm_max_retries=0, llm_json_repair_retries=1)

    with pytest.raises(LLMError):
        __import__("asyncio").run(client.invoke_json("sys", "user", _Reply))
    assert stub.calls == 2             # 首次 + 1 次修复重试

    strict = _client(_FailingLLM(0, ConnectionError("unused"), content="这不是 JSON"),
                     _settings, llm_max_retries=0, llm_json_repair_retries=0)
    with pytest.raises(LLMError):
        __import__("asyncio").run(strict.invoke_json("sys", "user", _Reply))
    assert strict._llm.calls == 1


def test_inflight_counter_returns_to_zero(_settings, no_sleep):
    stub = _FailingLLM(0, ConnectionError("unused"))
    client = _client(stub, _settings)

    __import__("asyncio").run(client.invoke_text("sys", "user"))
    assert client.inflight == 0
