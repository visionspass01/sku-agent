"""验收脚本：两轮对话后打印工具/中台/LLM 延迟分位（修复前后对比用）。

用法：.\\\\.venv\\\\Scripts\\\\python.exe scripts\\\\verify_latency.py
"""

from __future__ import annotations

import json

import httpx

BASE = "http://127.0.0.1:8000"


def main() -> None:
    with httpx.Client(base_url=BASE, timeout=300, trust_env=False) as client:
        token = client.post("/api/auth/login",
                            json={"username": "admin", "password": "hhw123"}).json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        for i in (1, 2):
            resp = client.post("/api/chat", headers=headers, json={
                "message": "ST-01-C1-M 当前可用库存", "include_query_plan": True,
            }).json()
            detail = resp.get("run_detail") or {}
            steps = [(s["tool"], s["elapsed_ms"], s["agent_cached"]) for s in detail.get("steps", [])]
            print(f"轮{i}: 步骤={steps}")
            print(f"     turn_elapsed={detail.get('elapsed_ms')}ms llm={detail.get('llm')}")

        metrics = client.get("/api/metrics", headers=headers).json()
        print("\n--- 延迟分位（修复后）")
        for name, payload in metrics["latency"]["categories"].items():
            if payload["count"]:
                print(f"  {name:<10} count={payload['count']:<4} "
                      f"p50={payload['p50']:<9} p95={payload['p95']}")
        print("\nlabels:", json.dumps(metrics["latency"]["labels"], ensure_ascii=False))


if __name__ == "__main__":
    main()
