"""清理档案的历史重复行（同一 content_hash 多行），并把 markdown_path 指向同一份主文档。

为什么要用它：`uq_analysis_hash` 唯一索引只有在「表内没有重复」时才能建起来，
而早期版本（P0-2 修复之前）确实可能写下重复行。跑完本脚本后重启服务，
`ensure_columns()` 会自动把普通索引升级为唯一索引，从物理上挡住并发写重复。

用法：
    .\\.venv\\Scripts\\python.exe scripts\\dedupe_analyses.py            # 只报告
    .\\.venv\\Scripts\\python.exe scripts\\dedupe_analyses.py --apply     # 真删
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import select  # noqa: E402

from app.db.engine import init_schema, session_scope  # noqa: E402
from app.db.models import AnalysisResult  # noqa: E402
from app.memory import analysis_merge, org_memory  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="清理档案历史重复（同一内容指纹多行）")
    parser.add_argument("--apply", action="store_true", help="真的删除重复行（默认只报告）")
    args = parser.parse_args()

    init_schema()
    keep_map: dict[str, str] = {}
    with session_scope() as session:
        rows = list(session.scalars(
            select(AnalysisResult).order_by(AnalysisResult.created_at, AnalysisResult.analysis_id)
        ))
        grouped: dict[str, list[AnalysisResult]] = defaultdict(list)
        for row in rows:
            if row.content_hash:
                grouped[row.content_hash].append(row)
        duplicates = {h: items for h, items in grouped.items() if len(items) > 1}
        print(f"档案总数 {len(rows)}，重复指纹 {len(duplicates)} 组")
        for content_hash, items in duplicates.items():
            keeper = items[0]
            canonical = org_memory.canonical_md_path(keeper.department, keeper.question)
            keep_map[content_hash] = canonical.as_posix()
            print(f"  - {content_hash[:12]}：{len(items)} 行 → 保留 {keeper.analysis_id}"
                  f"（{keeper.department}/{keeper.question[:24]}），主文档 {canonical.name}")
            if not args.apply:
                continue
            for extra in items[1:]:
                session.delete(extra)
            keeper.markdown_path = str(canonical)

    if not args.apply:
        print("\n（只报告模式；加 --apply 才会删除）")
        return 0

    # 重建每个受影响主题的主文档（entries 从 DB 行重建，不依赖旧文件）
    seen = set()
    for content_hash in keep_map:
        with session_scope() as session:
            row = session.scalars(
                select(AnalysisResult).where(AnalysisResult.content_hash == content_hash)
            ).first()
        if row is None:
            continue
        topic = org_memory.topic_key(row.department, row.question)
        if topic in seen:
            continue
        seen.add(topic)
        result = org_memory.rerender_topic(row.department, row.question)
        print(f"  重渲染 {result.get('canonical', '')} → entries={result.get('entries')}")

    # 把仍指向旧路径的行统一到主文档（合并语义：同一问题的多次分析共用一个文件）
    moved = 0
    with session_scope() as session:
        for row in session.scalars(select(AnalysisResult)):
            canonical = org_memory.canonical_md_path(row.department, row.question).as_posix()
            if row.markdown_path != canonical:
                row.markdown_path = canonical
                moved += 1
    print(f"统一 markdown_path：{moved} 行 → 主文档")
    analysis_merge.log_event({"outcome": "dedupe_applied", "groups": len(keep_map), "moved": moved})
    print("\n完成。重启服务后 ensure_columns() 会把索引升级为唯一索引。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
