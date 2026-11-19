"""第四轮 W7：中台 `POST /api/v1/image/results` 的 upsert 语义（P2-14）。

过去同 `(task_id, slot)` 一律跳过（算 deduped），于是"Agent 重试后重发的新结果"被静默丢掉，
中台永远留着上一轮的旧图。现在：
- 不存在 → 新建（saved）
- 已存在且 sha256 相同 → deduped（真的重复）
- 已存在但内容变了 → 覆盖（updated）
"""

from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def platform_db(tmp_path_factory):
    from tests.image_platform_support import init_platform_db, reset_platform_engine

    tmp = tmp_path_factory.mktemp("w7_results")
    init_platform_db(tmp)
    yield tmp
    reset_platform_engine()


def _platform_client():
    from tests.image_platform_support import platform_client

    return platform_client()


def _headers():
    from tests.image_platform_support import TOKEN

    return dict(TOKEN)


def _payload(*, task_id: str, request_id: str, sha256: str, prompt: str = "白底主图") -> dict:
    return {
        "request_id": request_id, "batch_id": "batch-w7", "product_id": "P-1002",
        "task_id": task_id, "platform": "淘系", "aspect": "1:1", "count": 1,
        "requester": "worker1", "trace_id": "trace-w7",
        "results": [{
            "slot": 1, "role": "主图", "prompt": prompt, "negative_prompt": "",
            "model": "fake-placeholder", "sha256": sha256, "mime": "image/png",
            "width": 1024, "height": 1024, "bytes": 1234, "storage": "local",
            "storage_key": f"outputs/{sha256[:8]}.png", "critic_score": 90,
            "critic_issues": [], "cost_cny": 0.02, "elapsed_ms": 100, "status": "done",
        }],
    }


def test_new_result_is_saved(platform_db):
    client = _platform_client()
    with client as c:
        body = c.post("/api/v1/image/results",
                      json=_payload(task_id="t-w7-1", request_id="req-w7-1",
                                    sha256="a" * 64),
                      headers=_headers()).json()
    assert body["saved"] == 1 and body["deduped"] == 0 and body["updated"] == 0


def test_identical_resend_is_deduped(platform_db):
    client = _platform_client()
    payload = _payload(task_id="t-w7-2", request_id="req-w7-2", sha256="b" * 64)
    with client as c:
        first = c.post("/api/v1/image/results", json=payload, headers=_headers()).json()
        second = c.post("/api/v1/image/results", json=payload, headers=_headers()).json()
    assert first["saved"] == 1
    assert second["deduped"] == 1 and second["saved"] == 0


def test_retry_resend_overwrites_previous_result(platform_db):
    """重试后重发（同 task/slot，内容变了）→ 覆盖，不丢新结果。"""
    client = _platform_client()
    old = _payload(task_id="t-w7-3", request_id="req-w7-3", sha256="c" * 64, prompt="旧提示词")
    new = _payload(task_id="t-w7-3", request_id="req-w7-3b", sha256="d" * 64, prompt="新提示词")
    with client as c:
        assert c.post("/api/v1/image/results", json=old, headers=_headers()).json()["saved"] == 1
        body = c.post("/api/v1/image/results", json=new, headers=_headers()).json()
        rows = c.get("/api/v1/image/results", params={"product_id": "P-1002", "limit": 200},
                     headers=_headers()).json()["items"]
    assert body["updated"] == 1 and body["saved"] == 0 and body["deduped"] == 0

    mine = [row for row in rows if row["task_id"] == "t-w7-3"]
    assert len(mine) == 1, "同一 (task_id, slot) 只能有一行"
    assert mine[0]["sha256"] == "d" * 64, "覆盖后必须是新内容的 sha256"
    assert mine[0]["prompt"] == "新提示词"
    assert mine[0]["request_id"] == "req-w7-3b"


def test_multiple_slots_are_independent(platform_db):
    client = _platform_client()
    base = _payload(task_id="t-w7-4", request_id="req-w7-4", sha256="e" * 64)
    extra = dict(base["results"][0])
    extra.update({"slot": 2, "role": "细节", "sha256": "f" * 64})
    payload = {**base, "results": [base["results"][0], extra]}
    with client as c:
        first = c.post("/api/v1/image/results", json=payload, headers=_headers()).json()
        # 只重发 slot 2 且换了内容
        changed = dict(extra)
        changed["sha256"] = "1" * 64
        second = c.post("/api/v1/image/results",
                        json={**base, "results": [changed]}, headers=_headers()).json()
    assert first["saved"] == 2
    assert second["updated"] == 1
