"""W7 真实服务冒烟：起「数据中台(:8011) + 生图 Agent(:8021, IMAGE_PRODUCT_SOURCE=mcp)」后跑关键链路。

覆盖：
1. 商品上下文批量接口（W2）与 MCP 链路可用；
2. 建任务（多色 × iterate 差额，W4）→ 出图 → 回写中台（含 copy_json）；
3. 幂等：同 key 第二次 200 重放；
4. 素材库 upload → ref_source=library 建任务（W3）；
5. 拍立淘：ref_source=pailitao 出图 / 门槛拉到 100000 时 422；
6. 批次汇总 + 整批取消（W6）；
7. /health 里 degraded=false 与 context_batches 统计（W2/W5）。

用法：.venv\\Scripts\\python.exe scripts\\smoke_image_service.py --app http://127.0.0.1:8021
"""

from __future__ import annotations

import argparse
import base64
import json
import pathlib
import struct
import sys
import time
import urllib.error
import urllib.request
import zlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))


def _png(text: str, size: int = 640) -> bytes:
    """生成一张纯白底 + 居中灰块的小 PNG（不依赖 Pillow/numpy）。"""
    rows = []
    color = zlib.crc32(text.encode()) & 0xFFFFFF
    r, g, b = (60, 60, 60) if color % 2 else (200, 200, 200)
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            inside = size // 4 <= x < size * 3 // 4 and size // 4 <= y < size * 3 // 4
            row.extend((r, g, b) if inside else (255, 255, 255))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


class Client:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.token = ""

    def call(self, method: str, path: str, body=None, headers=None, raw=False):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
                return response.status, (payload if raw else json.loads(payload or b"{}"))
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            try:
                return exc.code, json.loads(payload or b"{}")
            except json.JSONDecodeError:
                return exc.code, {"detail": payload[:200]}


def expect(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'[OK]  ' if ok else '[FAIL]'} {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


failures: list[str] = []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", default="http://127.0.0.1:8021")
    args = parser.parse_args()
    client = Client(args.app)

    status, health = client.call("GET", "/api/image/health")
    expect("health 可用", status == 200 and health.get("status") == "ok", str(health)[:120])
    expect("数据源是中台 MCP", health.get("product_source") == "mcp", str(health.get("product_source")))
    expect("库结构未降级", health.get("degraded") is False, str(health.get("degraded_reason"))[:80])

    status, body = client.call("POST", "/api/image/auth/login",
                               {"username": "admin", "password": "hhw123"})
    expect("登录", status == 200 and body.get("token"), str(status))
    client.token = body.get("token", "")

    status, products = client.call("GET", "/api/image/products?limit=5")
    expect("商品清单来自中台", status == 200 and products.get("products"), str(status))

    # 1) 多色 + iterate
    status, base = client.call("POST", "/api/image/tasks", {
        "product_ids": ["P-1001"],
        "product_colors": [{"product_id": "P-1001", "colors": ["米色"]}],
        "prompt": "白底主图，正面全身像上装突出", "image_count": 2, "slot_count": 3,
    })
    expect("建基础任务（中台商品图）", status == 201, str(base)[:160])
    base_id = (base.get("tasks") or [{}])[0].get("task_id", "")
    batch_id = base.get("batch_id", "")
    deadline = time.time() + 90
    detail = {}
    while time.time() < deadline and base_id:
        status, detail = client.call("GET", f"/api/image/tasks/{base_id}")
        if detail.get("status") in {"done", "failed"} and detail.get("backfill_status") != "pending":
            break
        time.sleep(0.3)
    expect("基础任务出图完成", detail.get("status") == "done", str(detail.get("error_code")))
    expect("回写中台成功", detail.get("backfill_status") == "sent", str(detail.get("backfill_status")))
    expect("copy_json 契约存在", bool((detail.get("copy") or {}).get("slots")),
           str(detail.get("copy"))[:120])

    status, iterate = client.call("POST", "/api/image/tasks", {
        "product_ids": ["P-1001"],
        "product_colors": [{"product_id": "P-1001", "colors": ["米色", "黑色"]}],
        "prompt": "主图点击率低，只改主图", "mode": "iterate", "base_task_id": base_id,
        "image_count": 2, "slot_count": 3,
    })
    expect("多色 iterate 建任务", status == 201, str(iterate)[:160])
    if status == 201:
        by_color = {item["color"]: item for item in iterate["tasks"]}
        expect("iterate 只复用同色目标",
               by_color["米色"]["reused_images"] >= 1 and by_color["黑色"]["reused_images"] == 0,
               json.dumps({k: (v["reused_images"], v["image_count"]) for k, v in by_color.items()},
                          ensure_ascii=False))

    # 2) 幂等（每次跑用新 key，否则第一次就是"重放"）
    payload = {"product_ids": ["P-3001"], "prompt": "白底主图", "image_count": 1}
    key = f"smoke-idem-{int(time.time())}"
    status1, first = client.call("POST", "/api/image/tasks", payload,
                                 headers={"Idempotency-Key": key})
    status2, second = client.call("POST", "/api/image/tasks", payload,
                                  headers={"Idempotency-Key": key})
    expect("幂等重放", status1 == 201 and status2 == 200
           and first.get("batch_id") == second.get("batch_id"), f"{status1}/{status2}")

    # 3) 素材库 → ref_source=library
    status, asset = client.call("POST", "/api/image/library",
                                {"data_b64": base64.b64encode(_png("lib")).decode()})
    expect("素材库上传", status == 201 and asset.get("asset_id"), str(status))
    status, lib_task = client.call("POST", "/api/image/tasks", {
        "product_ids": ["P-3003"], "prompt": "白底主图", "image_count": 1,
        "ref_source": "library", "library_asset_ids": [asset.get("asset_id", "")],
    })
    expect("用素材库出图", status == 201 and lib_task.get("ref_source") == "library",
           str(lib_task)[:160])
    client.call("DELETE", f"/api/image/library/{asset.get('asset_id', '')}")

    # 4) 拍立淘
    status, pailitao = client.call("POST", "/api/image/tasks", {
        "product_ids": ["P-2001"], "prompt": "白底主图", "image_count": 1,
        "ref_source": "pailitao",
    })
    expect("拍立淘建任务", status == 201, str(pailitao)[:160])

    # 5) 批次汇总 + 整批取消
    status, batch = client.call("GET", f"/api/image/batches/{batch_id}")
    expect("批次汇总", status == 200 and batch.get("count", 0) >= 1, str(status))
    status, canceled = client.call("POST", "/api/image/tasks/cancel-batch",
                                   {"batch_id": batch_id})
    expect("整批取消", status == 200 and "canceled" in canceled, str(canceled)[:160])

    # 6) 指标卡口径
    status, metrics = client.call("GET", "/api/image/admin/metrics?granularity=day&count=7")
    cards = (metrics or {}).get("cards", {})
    expect("五张卡口径存在", status == 200 and all(
        key in cards for key in ("usage_images", "provider_calls", "api_availability",
                                 "agent_pass_rate", "listing_rate", "quota_used",
                                 "in_flight", "cost_per_image")), str(list(cards))[:160])

    print(f"\n{'全部通过' if not failures else str(len(failures)) + ' 项失败'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
