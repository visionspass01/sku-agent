"""从数据库行重渲染档案主文档（灾难恢复 / 合并出错后的兜底）。

档案的事实源是 `analysis_result` 行（问题/结论/指标/证据都是列），markdown 只是投影。
所以任何「合并写坏了」的情况都可以用本脚本按事实源重建主文档，取证材料不会丢。

用法：
    .\\.venv\\Scripts\\python.exe scripts\\render_analysis_md.py                 # 全部主题
    .\\.venv\\Scripts\\python.exe scripts\\render_analysis_md.py --dept 男装
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import select  # noqa: E402

from app.db.engine import init_schema, session_scope  # noqa: E402
from app.db.models import AnalysisResult  # noqa: E402
from app.memory import org_memory  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="按 DB 事实源重渲染档案主文档")
    parser.add_argument("--dept", default="", help="只看某个部门（默认全部）")
    args = parser.parse_args()

    init_schema()
    with session_scope() as session:
        rows = list(session.scalars(select(AnalysisResult)))
    topics: dict[tuple[str, str], str] = {}
    for row in rows:
        if args.dept and row.department != args.dept:
            continue
        dept = "" if row.department in ("", org_memory.ALL_DEPARTMENTS_LABEL) else row.department
        topics[(dept, row.question)] = row.analysis_id

    print(f"待重渲染主题 {len(topics)} 个（DB 行 {len(rows)}）")
    written = 0
    for (dept, question), _aid in topics.items():
        result = org_memory.rerender_topic(dept or None, question)
        if result.get("written"):
            written += 1
            print(f"  ✓ {Path(result['canonical']).name}（entries={result.get('entries')}）")
        else:
            print(f"  - 跳过：{result.get('reason')} {question[:24]}")
    print(f"\n重渲染完成：{written}/{len(topics)}")
    return 0 if written == len(topics) else 1


if __name__ == "__main__":
    raise SystemExit(main())
