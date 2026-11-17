"""生成 Agent 侧的能力目录快照（`src/app/tools/capabilities.snapshot.json`）。

为什么是"生成到仓库里"而不是运行时全靠接口：
Agent 必须能**离线启动**（CI、无中台的本地开发、中台重建中的窗口期）。快照提供兜底，
中台提供真相；`tests/test_capabilities_sync.py` 保证两者一致，`/api/health.capabilities`
在运行时标注是否漂移。

用法：
    python scripts/gen_capabilities_snapshot.py            # 生成/更新快照
    python scripts/gen_capabilities_snapshot.py --check    # 只校验（CI 用，不一致返回 1）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

SNAPSHOT = ROOT / "src" / "app" / "tools" / "capabilities.snapshot.json"


def build_snapshot() -> dict:
    from data_platform.capabilities import RESERVED_GROUPS, catalog, validate

    problems = validate()
    if problems:
        raise SystemExit("能力目录自检未通过：" + "；".join(problems))
    data = catalog()
    return {
        "_comment": (
            "由 scripts/gen_capabilities_snapshot.py 生成，请勿手改。"
            "真相在中台 src/data_platform/capabilities.py。"
        ),
        "version": data["version"],
        "groups": data["groups"],
        "reserved_groups": RESERVED_GROUPS,
        "capabilities": data["capabilities"],
    }


def render(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成/校验 Agent 能力目录快照")
    parser.add_argument("--check", action="store_true", help="只校验，不写文件")
    args = parser.parse_args(argv)

    payload = build_snapshot()
    text = render(payload)
    if args.check:
        if not SNAPSHOT.exists():
            print(f"[FAIL] 快照不存在：{SNAPSHOT}")
            return 1
        current = SNAPSHOT.read_text(encoding="utf-8")
        if current != text:
            print("[FAIL] 快照与中台能力目录不一致，请运行 python scripts/gen_capabilities_snapshot.py")
            return 1
        print("[OK] 快照与中台能力目录一致")
        return 0

    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(text, encoding="utf-8")
    tools = sum(len(v) for v in payload["groups"].values())
    print(f"[OK] 已写入 {SNAPSHOT.relative_to(ROOT)}："
          f"{len(payload['capabilities'])} 个能力 / {len(payload['groups'])} 个分组 / {tools} 个工具")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
