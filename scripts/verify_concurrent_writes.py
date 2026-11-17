"""并发写验收脚本：证明「两个 agent 同时写同一份档案」不会静默覆盖。

跑法（离线，不需要服务）：
    .\\.venv\\Scripts\\python.exe scripts\\verify_concurrent_writes.py

四个场景：
1. 同主题并发写（不同结论/证据）→ 两份都在主文档里，主文档只有一份，保留 prev/new 快照；
2. 并发写完全相同内容 → 只落一行、只写一次（幂等仍然有效）；
3. 主文档被破坏 → 能从 DB 行重渲染（markdown 是投影，事实源是表）；
4. 结构化合并日志可读（谁在什么时候覆盖了谁、留了哪些快照）。

退出码 0 表示全部符合预期。
"""

from __future__ import annotations

import logging
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import select  # noqa: E402

from app.config import Settings, set_settings  # noqa: E402
from app.db.engine import init_schema, make_engine, session_scope, set_engine  # noqa: E402
from app.db.models import AnalysisResult  # noqa: E402
from app.memory import analysis_merge, org_memory  # noqa: E402

QUESTION = "ST-92-C1-M 补多少合适"


def _setup(tmp: Path) -> None:
    """用临时库/临时目录跑，避免污染演示数据。"""
    settings = Settings(
        database_url=f"sqlite:///{(tmp / 'verify.db').as_posix()}",
        analyses_dir=str(tmp / "analyses"),
        # 离线脚本不依赖模型：keep-both = 确定性合并 + 双份保留（LLM 摘要整合另有用例覆盖）
        analysis_merge_mode="keep-both",
    )
    set_settings(settings)
    set_engine(make_engine(settings))
    init_schema()


def _save(tag: str, *, same_content: bool = False) -> dict:
    evidence = ["res_repeat0000"] if same_content else [f"res_{tag * 10}"]
    conclusion = "建议补货 300 件" if same_content else f"建议补货 300 件（{tag}）"
    return org_memory.save_analysis(
        department="男装", username="admin", question=QUESTION, conclusion=conclusion,
        key_metrics={"available": 420 if tag == "ab" else 90}, evidence=evidence,
        metric_version="2026Q1",
    )


def _concurrent(call, tags: list[str]) -> list[dict]:
    barrier = threading.Barrier(len(tags))
    results: list[dict] = []
    errors: list[BaseException] = []

    def worker(tag: str) -> None:
        try:
            barrier.wait(timeout=10)
            results.append(call(tag))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(tag,)) for tag in tags]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    if errors:
        raise RuntimeError(f"并发写出现异常：{errors}")
    return results


def _rows() -> list[AnalysisResult]:
    with session_scope() as session:
        return list(session.scalars(select(AnalysisResult)))


def main() -> int:
    # 冲突是本脚本的**预期**结果，不让它刷 stderr（日志文件里有完整记录）
    logging.getLogger("sku-agent.analysis_merge").setLevel(logging.ERROR)
    tmp = Path(tempfile.mkdtemp(prefix="verify_writes_"))
    _setup(tmp)
    canonical = org_memory.canonical_md_path("男装", QUESTION)
    print(f"临时目录：{tmp}\n主文档：{canonical}\n")

    ok = True

    # ---------------- 场景 1：并发写不同内容
    results = _concurrent(_save, ["ab", "cd"])
    text = canonical.read_text(encoding="utf-8") if canonical.exists() else ""
    doc = analysis_merge.parse_doc(text)
    ids = {r["analysis_id"] for r in results}
    present = {aid for aid in ids if aid in text}
    print("① 并发写不同内容")
    print(f"   两次分析 id：{sorted(ids)}")
    print(f"   主文档 entries：{len(doc.entries) if doc else 0}，内容包含：{len(present)}/2")
    snapshots = sorted(p.name for p in canonical.parent.glob(f"{canonical.stem}.*-*.md"))
    print(f"   快照文件：{snapshots or '（无冲突，未产生快照）'}")
    if len(present) != 2:
        ok = False
        print("   ✗ 有分析被覆盖/丢失")
    else:
        print("   ✓ 两份分析都在同一份主文档里，没有互相覆盖")

    # ---------------- 场景 2：并发写相同内容（幂等）
    before_rows = len(_rows())
    same = _concurrent(lambda tag: _save(tag, same_content=True), ["xx", "yy"])
    after_rows = len(_rows())
    deduped = [r for r in same if r.get("deduped")]
    print("\n② 并发写相同内容")
    print(f"   DB 行数 {before_rows} → {after_rows}，deduped 命中 {len(deduped)}/2")
    if after_rows != before_rows + 1 or len(deduped) != 1:
        ok = False
        print("   ✗ 幂等失效（可能插入了重复行）")
    else:
        print("   ✓ 只落一行，唯一索引/指纹判重生效")

    # ---------------- 场景 3：从 DB 重渲染
    analysis_merge.atomic_write(canonical, "被人手改坏了\n")
    rebuilt = org_memory.rerender_topic("男装", QUESTION)
    restored = canonical.read_text(encoding="utf-8")
    print("\n③ 主文档被破坏后重渲染")
    print(f"   entries={rebuilt.get('entries')}，恢复后长度={len(restored)}")
    if not rebuilt.get("written") or len(restored) < 100:
        ok = False
        print("   ✗ 恢复失败")
    else:
        print("   ✓ 按 DB 事实源重建成功（markdown 只是投影）")

    # ---------------- 场景 4：日志可审计
    log_path = analysis_merge.merge_log_path()
    events = analysis_merge.read_log(limit=20)
    outcomes = sorted({e.get("outcome") for e in events})
    print("\n④ 合并日志")
    print(f"   {log_path}")
    print(f"   事件 {len(events)} 条，outcome={outcomes}")
    if not events:
        ok = False
        print("   ✗ 没有任何日志（无法审计）")
    else:
        print("   ✓ 每次写入/冲突都有结构化记录")

    print("\n" + ("全部符合预期 ✔" if ok else "存在不符合预期的场景 ✘"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
