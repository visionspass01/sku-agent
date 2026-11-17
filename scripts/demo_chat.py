"""CLI 演示：与 Agent 进行自然语言对话（问数 / 诊断 / 自动补货）。

用法：
  python scripts/demo_chat.py --user admin --password hhw123 --message "ST-01-C1-M 当前可用库存和预计售罄时间"
  python scripts/demo_chat.py --user admin --password hhw123 --session <sid> --message "那需要补货吗？"
"""

from __future__ import annotations

import argparse
import json

import httpx

BASE = "http://127.0.0.1:8000"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default="hhw123")
    parser.add_argument("--session", default=None)
    parser.add_argument("--message", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    with httpx.Client(timeout=180) as client:
        login = client.post(f"{BASE}/api/auth/login",
                            json={"username": args.user, "password": args.password})
        login.raise_for_status()
        token = login.json()["token"]
        headers = {"Authorization": f"Bearer {token}"}

        payload = {"message": args.message}
        if args.session:
            payload["session_id"] = args.session
        resp = client.post(f"{BASE}/api/chat", headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    print(f"[session] {data['session_id']}")
    print(f"[user] {args.message}")
    print(f"[agent] {data['reply']}")
    trace = data.get("tool_trace") or []
    if trace:
        print("\n[tool_trace]")
        for t in trace:
            if t.get("type") == "call":
                print(f"  → {t.get('tool')} {json.dumps(t.get('args'), ensure_ascii=False)}")
            else:
                print(f"  ← {t.get('tool')}: {str(t.get('result'))[:180]}")


if __name__ == "__main__":
    main()
