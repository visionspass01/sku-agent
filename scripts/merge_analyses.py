"""处理「待合并」队列：把主文档里的多次分析整合成一段结论摘要（可重复运行）。

什么会进队列：主文档写到一半拿不到短锁、CAS 反复冲突、或写完之后需要 LLM 整合摘要。
工具层每次沉淀后会自动 drain 少量；本脚本用来一次性清空（离线/运维场景）。

用法：
    .\\.venv\\Scripts\\python.exe scripts\\merge_analyses.py            # 最多处理 20 条
    .\\.venv\\Scripts\\python.exe scripts\\merge_analyses.py --limit 100
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from app.db.engine import init_schema  # noqa: E402
from app.memory import analysis_merge, org_memory  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="处理档案待合并队列")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    init_schema()
    pending = analysis_merge.list_pending(args.limit)
    print(f"待合并 {len(pending)} 条（日志：{analysis_merge.merge_log_path()}）")
    for path in pending:
        payload = analysis_merge.load_pending(path) or {}
        print(f"  - {path.name} kind={payload.get('kind')} reason={payload.get('reason')} "
              f"topic={payload.get('topic')} canonical={Path(str(payload.get('canonical'))).name}")

    result = asyncio.run(org_memory.adrain_pending_merges(limit=args.limit))
    print("\n结果：" + json.dumps(result, ensure_ascii=False))
    for event in analysis_merge.read_log(limit=10):
        print(f"  [{event.get('at')}] {event.get('outcome')} {event.get('canonical', '')} "
              f"{event.get('reason', '')}")
    return 0 if result.get("failed", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
