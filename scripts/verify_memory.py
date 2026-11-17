"""手工验收脚本：两轮真实对话 + 记忆快照（需本地 Agent:8000、数据中台:8001 与 DEEPSEEK_API_KEY）。

用法：
    .\\.venv\\Scripts\\python.exe scripts\\verify_memory.py
"""

from __future__ import annotations

import json
import sys

import httpx

BASE = "http://127.0.0.1:8000"


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=300) as client:
        login = client.post("/api/auth/login", json={"username": "admin", "password": "hhw123"})
        login.raise_for_status()
        token = login.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        first = client.post("/api/chat", headers=headers, json={
            "message": "ST-06-C1-M 当前可用库存和预计售罄天数是多少？",
            "include_query_plan": True,
        })
        first.raise_for_status()
        r1 = first.json()
        print(f"turn1 session={r1['session_id']} ws={r1['memory'].get('working_version')} "
              f"facts={r1['memory'].get('facts_version')} cited={r1.get('cited_result_ids')}")
        print("reply1:", r1["reply"][:160].replace("\n", " "))

        second = client.post("/api/chat", headers=headers, json={
            "session_id": r1["session_id"],
            "message": "这个款需要补货吗？大概补多少",
            "include_query_plan": True,
        })
        second.raise_for_status()
        r2 = second.json()
        plan = r2.get("query_plan") or {}
        print(f"turn2 ws={r2['memory'].get('working_version')} "
              f"resolved={plan.get('resolved_from')} skus={plan.get('entities', {}).get('skus')} "
              f"conf={plan.get('confidence')}")
        print("keywords:", (plan.get("retrieval_query") or {}).get("keywords", [])[:10])
        print("reply2:", r2["reply"][:200].replace("\n", " "))

        snap = client.get(f"/api/memory/sessions/{r1['session_id']}", headers=headers)
        snap.raise_for_status()
        body = snap.json()
        ws = body.get("working_set") or {}
        print("\n--- memory snapshot ---")
        print("ws_skus   =", list((ws.get("skus") or {}).keys()))
        print("ws_metrics=", list((ws.get("metrics") or {}).keys()))
        print("facts_ver =", body.get("facts_version"), "trigger=", body.get("facts_trigger"))
        print("facts_card=", (body.get("facts_card") or "")[:200].replace("\n", " "))
        print("tool_rows =", len(body.get("recent_tool_results") or []),
              "latest_plan=", (body.get("latest_query_plan") or {}).get("plan_id"),
              "turn_count=", body.get("turn_count"))

        results = body.get("recent_tool_results") or []
        if results:
            print("last_result:", json.dumps(results[0], ensure_ascii=False))

        # 本轮运行详情（可观测性）
        detail = r2.get("run_detail") or r1.get("run_detail") or {}
        memory = detail.get("memory") or {}
        print("\n--- run_detail（第 2 轮） ---")
        print("turn_id   =", detail.get("turn_id"), "elapsed_ms =", detail.get("elapsed_ms"))
        plan = detail.get("plan") or {}
        print("plan      =", plan.get("plan_id"), plan.get("intent"), plan.get("confidence"))
        for step in detail.get("steps") or []:
            print(f"  step{step.get('seq')}: {step.get('tool')} {step.get('args')} "
                  f"{step.get('elapsed_ms')}ms {step.get('result_id')} as_of={step.get('as_of')} "
                  f"truncated={step.get('truncated')}")
        print("llm       =", detail.get("llm"))
        print("memory    =", {k: v for k, v in memory.items() if k != "context"},
              "| context_facts_chars =", len((memory.get("context") or {}).get("facts_card") or ""))
        print("permission=", detail.get("permission"))
        print("references=", (detail.get("references") or {}).get("result_ids"))
        hist = client.get(f"/api/chat/{r1['session_id']}", headers=headers).json()
        replay = [m for m in hist.get("messages", []) if m.get("run_detail")]
        print("history run_detail 回放条数 =", len(replay))
    return 0


if __name__ == "__main__":
    sys.exit(main())
