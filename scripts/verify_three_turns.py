"""三轮对话验收：run_detail / 事实卡 / 历史会话 message_count / 记忆面板数据。"""

from __future__ import annotations

import httpx

BASE = "http://127.0.0.1:8000"
QUESTIONS = [
    "ST-01-C1-M 当前可用库存",
    "这个款要补货吗",
    "那它最近7天销量怎么样",
]


def main() -> None:
    with httpx.Client(base_url=BASE, timeout=300) as c:
        token = c.post("/api/auth/login",
                       json={"username": "admin", "password": "hhw123"}).json()["token"]
        h = {"Authorization": f"Bearer {token}"}
        sid = None
        for i, q in enumerate(QUESTIONS, 1):
            r = c.post("/api/chat", headers=h,
                       json={"session_id": sid, "message": q, "include_query_plan": True}).json()
            sid = r["session_id"]
            d = r.get("run_detail") or {}
            plan = d.get("plan") or {}
            mem = d.get("memory") or {}
            llm = d.get("llm") or {}
            print(f"turn{i}: plan={plan.get('intent')} conf={plan.get('confidence')} "
                  f"steps={len(d.get('steps') or [])} facts_v={mem.get('facts_version')} "
                  f"delta={mem.get('facts_delta')} "
                  f"tokens={llm.get('input_tokens', 0) + llm.get('output_tokens', 0)}")

        sessions = c.get("/api/chat/sessions", headers=h).json()
        print("sessions:", [(it["title"][:14], it["message_count"]) for it in sessions["items"][:5]])

        mem = c.get(f"/api/memory/sessions/{sid}", headers=h).json()
        print("memory: facts_v=", mem["facts_version"], "working_v=", mem["working_version"],
              "turns=", mem["turn_count"], "card_chars=", len(mem["facts_card"]))
        print("card_head:", mem["facts_card"][:100].replace("\n", " "))

        hist = c.get(f"/api/chat/{sid}", headers=h).json()
        print("history messages:", len(hist["messages"]),
              "run_detail 条数:", sum(1 for m in hist["messages"] if m.get("run_detail")))


if __name__ == "__main__":
    main()
