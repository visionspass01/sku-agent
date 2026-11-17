"""知识库增量更新演示。

用法：
  python scripts/update_kb.py                 # 常规增量更新（无变更则全部 skip）
  python scripts/update_kb.py --edit-sim      # 模拟修改一篇 SOP 后增量重建（只重建该文档）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from app.config import KB_DIR, get_settings  # noqa: E402
from app.db.engine import init_schema, session_scope  # noqa: E402
from app.rag.index import build_index  # noqa: E402

MARKER = "（增量更新演示：规则追加于 {ts}）"


def _simulate_edit() -> None:
    import datetime as dt

    target = KB_DIR / "sop-clearance.md"
    text = target.read_text(encoding="utf-8")
    marker_line = MARKER.format(ts=dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    target.write_text(text.rstrip() + f"\n\n## 增量追加\n- {marker_line}\n", encoding="utf-8")
    print(f"[kb] 已修改 {target.name}（追加一行，用于演示增量更新只重建变更文档）")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--edit-sim", action="store_true", help="模拟修改文档后增量重建")
    args = parser.parse_args()

    settings = get_settings()
    settings.ensure_dirs()
    init_schema()

    if args.edit_sim:
        _simulate_edit()

    with session_scope() as session:
        stats = build_index(session)

    print("=" * 60)
    print("知识库索引更新结果")
    print(f"  新增文档: {stats.added or '无'}")
    print(f"  更新文档: {stats.updated or '无'}")
    print(f"  跳过文档: {len(stats.skipped)} 篇（sha256 未变化，不重新分块/向量化）")
    print(f"  删除文档: {stats.deleted or '无'}")
    print(f"  chunk 总数: {stats.chunks}")
    print("=" * 60)


if __name__ == "__main__":
    main()
