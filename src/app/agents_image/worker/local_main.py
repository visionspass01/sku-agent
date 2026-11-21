"""本地独立运行入口（不启动 FastAPI、不连数据库）：

    python -m app.agents_image.worker.local_main --input req.json --output out.json

它证明"第三方调用"这一层可以脱离 Web 进程独立跑（SCF 函数用的是同一份 `execute()`），
也是 CI / 端到端脚本验证 remote 链路的入口。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from typing import Any

from app.agents_image.worker.contract import WorkerRequest
from app.agents_image.worker.runner import execute, result_to_json


def _load(path: str) -> dict[str, Any]:
    text = pathlib.Path(path).read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise SystemExit("入参文件必须是 JSON 对象（WorkerRequest）")
    return data


async def _run(request_data: dict[str, Any]) -> dict[str, Any]:
    request = WorkerRequest.model_validate(request_data)
    result = await execute(request)
    return result_to_json(result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="独立生图调用 worker（本地入口）")
    parser.add_argument("--input", required=True, help="WorkerRequest JSON 文件")
    parser.add_argument("--output", default="", help="把 WorkerResult JSON 写到该文件")
    args = parser.parse_args(argv)
    payload = _load(args.input)
    result = asyncio.run(_run(payload))
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        pathlib.Path(args.output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text + "\n")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover —— CLI 入口
    raise SystemExit(main())
