"""第十二轮 Phase 1：独立 worker 的契约 / 执行 / 取图 / 三个入口。

覆盖点：
- 契约版本与必填校验（父/子契约不一致时快速失败）；
- `execute()`：出图 → 门禁 → 落存储 → 返回 `storage_key` / 像素统计；
- 门禁不过：图落到 `blocked/` 前缀、`kind=blocked`；
- 存储失败：`ok=false` 且**成本/发起量照带**（父进程据此记账，不出现"有图无路径"）；
- 截止时间：过了截止时间 → `IMG_TIMEOUT`；`return_bytes=True` 回传 base64；
- 取图三种来源（storage_key / inline / url）与超时预算；
- 三入口：本地 CLI（无 DB）、HTTP（Bearer）、SCF `main_handler`。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.agents_image.png import placeholder_png
from app.agents_image.worker import contract
from app.agents_image.worker.contract import WorkerRef, WorkerRequest
from app.agents_image.worker.refs import load_refs
from app.agents_image.worker.runner import execute
from app.agents_image.worker.storage import MemoryStorage
from app.core.errors import ImageError

REF_PNG = placeholder_png("worker-ref", 128, 128)
REF_B64 = base64.b64encode(REF_PNG).decode("ascii")


def _store_with_ref() -> tuple[MemoryStorage, str]:
    store = MemoryStorage()
    key = store.put(REF_PNG, kind="ref", mime="image/png").key
    return store, key


def _request(key: str, **overrides) -> WorkerRequest:
    base = dict(task_id="20261121010101w001", trace_id="trace-1",
                idempotency_key="20261121010101w001:1:1", provider="fake",
                width=128, height=128,
                slots=[{"slot": 1, "prompt": "白底主图", "role": "主图", "n": 1}],
                refs=[{"kind": "storage_key", "value": key}],
                storage={"backend": "memory"},
                gate={"mode": "main", "aspect": "1:1"})
    base.update(overrides)
    return WorkerRequest(**base)


# ---------------------------------------------------------------- 契约
def test_contract_version_mismatch_is_rejected():
    with pytest.raises(Exception):
        WorkerRequest(version="0", task_id="t", slots=[{"slot": 1}])


def test_contract_requires_slots():
    with pytest.raises(Exception):
        WorkerRequest(task_id="t", slots=[])


def test_contract_defaults_and_json_round_trip():
    store, key = _store_with_ref()
    req = _request(key)
    dumped = req.model_dump(mode="json")
    again = WorkerRequest.model_validate(dumped)
    assert again.version == contract.WORKER_CONTRACT_VERSION
    assert again.gate.aspect == "1:1"
    assert again.storage.backend == "memory"


# ---------------------------------------------------------------- execute
def test_execute_stores_image_and_returns_path():
    store, key = _store_with_ref()
    result = asyncio.run(execute(_request(key), storage=store))
    assert result.ok is True
    assert result.provider_calls == 1 and result.generated == 1
    assert len(result.images) == 1
    image = result.images[0]
    assert image.kind == "output"
    assert image.storage == "memory"
    assert image.storage_key.startswith("20261121010101w001/slot1-")
    assert image.sha256 and image.bytes > 0
    assert image.gate.passed is True
    assert image.pixel.pixel_check is True and image.pixel.dominant_rgb
    # 落点真的可读回（同一后端）
    assert store.get(image.storage_key)


def test_execute_marks_blocked_images_under_blocked_prefix():
    store, key = _store_with_ref()
    # 规则要求长边 ≥ 4096：128px 的占位图必然过不了门禁 → 落到 blocked/ 前缀
    blocked_req = _request(
        key,
        rules={"min_px": 4096, "aspect": "1:1"},
        gate={"mode": "all", "aspect": "1:1", "autofix": False})
    result = asyncio.run(execute(blocked_req, storage=store))
    assert result.ok is True
    image = result.images[0]
    assert image.kind == "blocked"
    assert "blocked/" in image.storage_key
    assert image.gate.passed is False and image.gate.reasons
    assert image.blocked_reason
    assert store.get(image.storage_key)


def test_execute_gate_off_skips_gate_but_keeps_pixel_stats():
    store, key = _store_with_ref()
    result = asyncio.run(execute(_request(key, gate={"mode": "off"}), storage=store))
    assert result.ok is True
    image = result.images[0]
    assert image.kind == "output" and image.gate.passed is True
    assert image.gate.stats == {}                    # 没跑门禁
    assert image.pixel.pixel_check is True           # 仍然解码（Critic 要用主色）


def test_execute_storage_failure_keeps_cost_facts():
    inline_req = _request(
        "unused",
        refs=[{"kind": "inline", "value": REF_B64}])

    class _BrokenStorage(MemoryStorage):
        name = "broken"

        def put(self, data, **kwargs):
            raise ImageError("IMG_STORAGE_UNAVAILABLE", "COS 挂了", retryable=True)

    result = asyncio.run(execute(inline_req, storage=_BrokenStorage()))
    assert result.ok is False
    assert result.error is not None and result.error.code == "IMG_STORAGE_UNAVAILABLE"
    assert result.error.retryable is True
    # 图已经生成：成本/发起量必须带回去（父进程照记，不出现"有图无路径"）
    assert result.provider_calls == 1
    assert result.generated == 1
    assert result.images == []


def test_execute_expired_deadline_reports_timeout():
    store, key = _store_with_ref()
    result = asyncio.run(execute(_request(key, deadline_at=time.time() - 1), storage=store))
    assert result.ok is False
    assert result.error is not None and result.error.code == "IMG_TIMEOUT"
    # 截止时间在第一次调用前就命中：一次都没发出去
    assert result.provider_calls == 0
    assert result.generated == 0


def test_execute_return_bytes_for_debug():
    store, key = _store_with_ref()
    result = asyncio.run(execute(_request(key, return_bytes=True), storage=store))
    assert result.ok is True
    content = result.images[0].content
    assert content and base64.b64decode(content).startswith(b"\x89PNG")


def test_execute_idempotent_same_sha_same_key():
    store, key = _store_with_ref()
    first = asyncio.run(execute(_request(key), storage=store))
    second = asyncio.run(execute(_request(key), storage=store))
    assert first.images[0].storage_key == second.images[0].storage_key
    assert len(store._objects) == 2          # ref + 1 张 output（覆盖而非新增）


# ---------------------------------------------------------------- refs
def test_refs_storage_key_and_inline(monkeypatch):
    store, key = _store_with_ref()
    got = asyncio.run(load_refs([WorkerRef(kind="storage_key", value=key),
                                 WorkerRef(kind="inline", value=REF_B64)],
                                storage=store))
    assert len(got) == 2
    assert got[0]["data"] == REF_PNG and got[0]["source"] == "storage_key"
    assert got[1]["data"] == REF_PNG and got[1]["source"] == "inline"


def test_refs_missing_key_raises():
    with pytest.raises(ImageError) as info:
        asyncio.run(load_refs([WorkerRef(kind="storage_key", value="nope")],
                              storage=MemoryStorage()))
    assert info.value.code == "IMG_REFERENCE_EXPIRED"


def test_refs_budget_timeout(monkeypatch):
    from app.agents_image.worker import refs as refs_mod

    async def slow(url, *, timeout, client):
        await asyncio.sleep(3)
        return b"x"

    monkeypatch.setattr(refs_mod, "_fetch_url", slow)
    started = time.monotonic()
    with pytest.raises(ImageError) as info:
        asyncio.run(load_refs([WorkerRef(kind="url", value="https://example.invalid/a.png")],
                              storage=MemoryStorage(), budget_seconds=1))
    assert info.value.code == "IMG_UPSTREAM_MCP_UNAVAILABLE"
    assert time.monotonic() - started < 2.5


# ---------------------------------------------------------------- 三入口
def test_local_cli_runs_without_database(tmp_path):
    req_path = tmp_path / "req.json"
    out_path = tmp_path / "out.json"
    req_path.write_text(json.dumps({
        "task_id": "20261121010101w002", "provider": "fake",
        "width": 128, "height": 128,
        "slots": [{"slot": 1, "prompt": "白底主图", "role": "主图"}],
        "refs": [{"kind": "inline", "value": REF_B64}],
        "storage": {"backend": "memory"},
        "gate": {"mode": "main"},
    }), encoding="utf-8")
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, "-m", "app.agents_image.worker.local_main",
         "--input", str(req_path), "--output", str(out_path)],
        cwd=str(root), capture_output=True, text=True, timeout=180,
        encoding="utf-8", errors="replace",
        env={"PYTHONPATH": str(root / "src"), "PYTHONIOENCODING": "utf-8",
             "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
             "PATH": os.environ.get("PATH", "")},
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(out_path.read_text(encoding="utf-8"))
    assert out["ok"] is True
    assert out["images"][0]["storage_key"].startswith("20261121010101w002/slot1-")


def test_http_entrypoint_requires_token(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app.agents_image import config as image_config
    from app.agents_image.worker import http_app as http_mod

    monkeypatch.setenv("IMAGE_WORKER_TOKEN", "w-token")
    image_config.reset_image_settings()
    client = TestClient(http_mod.app)
    body = {
        "task_id": "20261121010101w003", "provider": "fake", "width": 128, "height": 128,
        "slots": [{"slot": 1, "prompt": "白底主图"}],
        "refs": [{"kind": "inline", "value": REF_B64}],
        "storage": {"backend": "memory"}, "gate": {"mode": "main"},
    }
    assert client.post("/generate", json=body).status_code == 401
    resp = client.post("/generate", json=body,
                       headers={"Authorization": "Bearer w-token"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert client.get("/healthz").json()["ok"] is True
    image_config.reset_image_settings()


def test_scf_main_handler_returns_result():
    from app.agents_image.worker.scf_main import main_handler

    event = {"task_id": "20261121010101w004", "provider": "fake",
             "width": 128, "height": 128,
             "slots": [{"slot": 1, "prompt": "白底主图"}],
             "refs": [{"kind": "inline", "value": REF_B64}],
             "storage": {"backend": "memory"}, "gate": {"mode": "main"}}
    result = main_handler(event, None)
    assert result["ok"] is True
    assert result["images"][0]["storage_key"].startswith("20261121010101w004/slot1-")


def test_scf_main_handler_api_gateway_style():
    from app.agents_image.worker.scf_main import main_handler

    event = {"body": json.dumps({
        "task_id": "20261121010101w005", "provider": "fake", "width": 128, "height": 128,
        "slots": [{"slot": 1, "prompt": "白底主图"}],
        "refs": [{"kind": "inline", "value": REF_B64}],
        "storage": {"backend": "memory"}, "gate": {"mode": "main"}})}
    resp = main_handler(event, None)
    assert resp["statusCode"] == 200
    parsed = json.loads(resp["body"])
    assert parsed["ok"] is True
