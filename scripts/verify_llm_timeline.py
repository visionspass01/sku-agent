"""实测一轮对话的时间构成：工具毫秒级、模型秒级（需服务在跑）。

用法（先启动 Agent，默认 8000 端口）：
    python scripts/verify_llm_timeline.py

做两件事：
1. 订阅 `POST /api/chat/stream`，打印每个事件的到达时刻与内容（重点看 `llm_start` /
   `llm_end` 是否在工具之间的空档里出现）；
2. 用 `run_detail.timeline` + `phases` 打印耗时构成，验证「工具 16ms、整轮 7.8s」
   的差值是模型调用，而不是工具在慢慢跑。

退出码非 0 表示断言失败（不会静默通过）。
"""

from __future__ import annotations

import json
import os
import sys
import time

import httpx

BASE = os.environ.get("AGENT_BASE", "http://127.0.0.1:8000")
USERNAME = os.environ.get("AGENT_USER", "admin")
PASSWORD = os.environ.get("AGENT_PASSWORD", "hhw123")
MESSAGE = os.environ.get(
    "AGENT_MESSAGE",
    "ST-01-C1-M 当前可用库存和预计售罄时间，顺便看 ST-02-C1-M 的补货建议",
)


def main() -> int:
    client = httpx.Client(base_url=BASE, timeout=120, trust_env=False)
    token = client.post("/api/auth/login", json={"username": USERNAME, "password": PASSWORD}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    events: list[tuple[float, str, dict]] = []
    started = time.perf_counter()
    done: dict = {}
    with client.stream("POST", "/api/chat/stream", json={"message": MESSAGE}, headers=headers) as resp:
        resp.raise_for_status()
        buffer = ""
        for chunk in resp.iter_text():
            buffer += chunk
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                event, data = "message", ""
                for line in block.splitlines():
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data += line[5:].strip()
                if not data:
                    continue
                payload = json.loads(data)
                events.append((time.perf_counter() - started, event, payload))
                if event == "done":
                    done = payload

    print(f"\n=== 事件到达时间线（{len(events)} 条）===")
    for at, event, payload in events:
        if event == "tick":
            continue
        brief = json.dumps(payload, ensure_ascii=False)
        print(f"{at:7.3f}s  {event:11s} {brief[:150]}")

    llm_events = [e for e in events if e[1] in ("llm_start", "llm_end")]
    tools = [e for e in events if e[1] in ("tool_start", "tool_end")]
    done_at = next((at for at, event, _ in events if event == "done"), 0.0)

    run_detail = done.get("run_detail") or {}
    phases = run_detail.get("phases") or {}
    print("\n=== run_detail 时间轴 ===")
    for item in run_detail.get("timeline") or []:
        kind = {"llm": "🧠 模型", "tool": "🔧 工具", "stage": "· 阶段"}.get(item["kind"], item["kind"])
        extra = ""
        if item["kind"] == "llm":
            extra = f"{item.get('kind_label', '')} in={item.get('input_tokens')} out={item.get('output_tokens')}"
        elif item["kind"] == "tool":
            extra = f"result_id={item.get('result_id')}"
        print(f"+{(item.get('offset_ms') or 0) / 1000:6.2f}s  {kind:6s} "
              f"{str(item.get('label'))[:34]:34s} {str(item.get('elapsed_ms')):>6} ms  {extra}")
    print(f"\n耗时构成：模型 {phases.get('llm_ms')} ms（{phases.get('llm_calls')} 次） · "
          f"工具 {phases.get('tools_ms')} ms（{phases.get('tool_calls')} 次） · "
          f"其它 {phases.get('other_ms')} ms · 整轮 {phases.get('total_ms')} ms")

    checks = [
        ("收到 llm_start 事件", any(e[1] == "llm_start" for e in events)),
        ("收到 llm_end 事件", any(e[1] == "llm_end" for e in events)),
        ("run_detail 带 timeline", bool(run_detail.get("timeline"))),
        ("run_detail 带 phases", bool(phases)),
        ("模型耗时 > 0（秒级归因生效）", (phases.get("llm_ms") or 0) > 0),
        ("工具合计 < 500ms（毫秒级）", (phases.get("tools_ms") or 0) < 500),
        ("整轮耗时与会话耗时一致", abs(done_at * 1000 - (run_detail.get("elapsed_ms") or 0)) < 1500),
        ("模型耗时占整轮一半以上", (phases.get("llm_ms") or 0) > (phases.get("total_ms") or 1) * 0.5),
    ]
    print("\n=== 断言 ===")
    failed = 0
    for label, ok in checks:
        print(f"  {'ok  ' if ok else 'FAIL'} {label}")
        failed += 0 if ok else 1
    print(f"\ntool_start/tool_end 事件数={len(tools)}，llm 事件数={len(llm_events)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
