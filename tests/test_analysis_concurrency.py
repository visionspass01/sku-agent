"""档案主文档的并发写（A1/A2）：CAS + 三份保留 + LLM 摘要整合。

回归背景：文件名曾是 `<分钟>-<标题 40 字>.md`，同一分钟、同一问题、证据不同的两次分析
会落到**同一个文件**上，后写的静默覆盖前一份正文（DB 两行、磁盘一份），没有任何日志。
现在：路径按 topic（部门+问题）派生；写入是「读时取哈希 → 短锁校验 → 原子替换」的 CAS，
冲突时保留原文档/新文档两份快照 + 结构化日志，再把从 DB 重建的全部 entries 确定性合并写回，
结论摘要的 LLM 整合在锁外异步进行。
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from app.db.engine import session_scope
from app.db.models import AnalysisResult
from app.memory import analysis_merge, org_memory


def _write(**overrides) -> dict:
    kwargs = dict(
        department="男装", username="admin",
        question="ST-92-C1-M 补多少合适", conclusion="建议补货 300 件（res_aaaaaaaaaa）",
        key_metrics={"available": 420}, evidence=["res_aaaaaaaaaa"],
        metric_version="2026Q1",
    )
    kwargs.update(overrides)
    return org_memory.save_analysis(**kwargs)


def _canonical(question: str = "ST-92-C1-M 补多少合适") -> Path:
    return org_memory.canonical_md_path("男装", question)


def _rows_for(query: str | None = None) -> list[AnalysisResult]:
    with session_scope() as session:
        rows = list(session.query(AnalysisResult).all())
    if query is None:
        return rows
    return [r for r in rows if r.question == query]


def _cleanup(query: str) -> None:
    """清掉本用例写入的档案（测试库是 session 级共享的）。"""
    with session_scope() as session:
        for row in session.query(AnalysisResult).filter_by(question=query).all():
            session.delete(row)


# ---------------------------------------------------------------- 路径与单次写入
def test_canonical_path_is_topic_based_not_time_based(seeded):
    question = "ST-92-C1-M 补多少合适"
    path = _canonical(question)
    # 路径里只有「部门 + 问题」，不含分钟时间戳/结论 → 同问题的多次分析必然落在同一份主文档
    assert path.parent.name == "男装"
    assert org_memory.topic_key("男装", question)[:8] in path.name
    assert org_memory.topic_key("男装", question) == org_memory.topic_key("男装", "  " + question + " ")


def test_first_write_creates_canonical_document(seeded):
    question = "首次写入测试：断码补货"
    _cleanup(question)
    saved = _write(question=question)
    path = _canonical(question)
    assert saved["deduped"] is False
    assert path.exists(), "提交成功后主文档必须已经落盘"
    text = path.read_text(encoding="utf-8")
    assert saved["analysis_id"] in text
    assert analysis_merge.MERGE_BEGIN in text
    _cleanup(question)


def test_same_topic_merges_into_one_document(seeded):
    """同问题的第二次分析：不新建文件、不覆盖，两份结论都在同一份主文档里。"""
    question = "同主题合并测试"
    _cleanup(question)
    first = _write(question=question, conclusion="结论一：补 300 件（res_bbbbbbbbbb）",
                   evidence=["res_bbbbbbbbbb"])
    second = _write(question=question, conclusion="结论二：补 480 件（res_cccccccccc）",
                    evidence=["res_cccccccccc"], key_metrics={"available": 100})
    text = _canonical(question).read_text(encoding="utf-8")
    assert first["analysis_id"] in text and second["analysis_id"] in text
    assert "结论一" in text and "结论二" in text
    files = [p for p in _canonical(question).parent.glob("同主题合并测试*")]
    assert len(files) == 1, f"同主题不应产生多个主文档：{files}"
    _cleanup(question)


def test_same_content_is_still_deduped(seeded):
    question = "幂等回归测试"
    _cleanup(question)
    a = _write(question=question, evidence=["res_dddddddddd"])
    b = _write(question=question, evidence=["res_dddddddddd"])
    assert b["deduped"] is True and b["analysis_id"] == a["analysis_id"]
    _cleanup(question)


def test_same_question_different_evidence_is_not_silently_overwritten(seeded):
    """评审缺陷的原始复现路径：同问题 + 同结论 + 证据不同（同分钟）→ 两份都要在。"""
    question = "静默覆盖回归"
    _cleanup(question)
    a = _write(question=question, evidence=["res_1111111111"])
    b = _write(question=question, evidence=["res_2222222222"])
    assert a["analysis_id"] != b["analysis_id"]
    text = _canonical(question).read_text(encoding="utf-8")
    assert "res_1111111111" in text and "res_2222222222" in text
    _cleanup(question)


# ---------------------------------------------------------------- 并发
def test_concurrent_writes_do_not_lose_entries(seeded):
    """两个线程同时写同一主题：都要落库、都要出现在主文档里，且互不覆盖。"""
    question = "并发写测试"
    _cleanup(question)
    barrier = threading.Barrier(2)
    results: list[dict] = []
    errors: list[BaseException] = []

    def worker(tag: str) -> None:
        try:
            barrier.wait(timeout=5)
            results.append(_write(
                question=question,
                conclusion=f"并发结论 {tag}（res_{tag * 10}）",
                evidence=[f"res_{tag * 10}"], key_metrics={"available": tag},
            ))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(tag,)) for tag in ("ab", "cd")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert not errors, errors
    assert len({r["analysis_id"] for r in results}) == 2
    text = _canonical(question).read_text(encoding="utf-8")
    for result in results:
        assert result["analysis_id"] in text, "并发写丢了某一份分析"
    doc = analysis_merge.parse_doc(text)
    assert doc is not None and len(doc.entries) >= 2
    _cleanup(question)


def test_hash_conflict_keeps_prev_and_new_snapshots(seeded):
    """读时哈希与写时不一致：保留原文档 + 新文档两份快照，并记冲突日志。"""
    question = "冲突留证测试"
    _cleanup(question)
    payload = _write_payload(question)
    canonical = Path(payload["canonical"])
    canonical.parent.mkdir(parents=True, exist_ok=True)
    analysis_merge.atomic_write(canonical, "# 别人后来写进来的内容\n")
    payload["expected_digest"] = "deadbeef" * 8          # 过期的期望哈希

    result = org_memory._write_document(payload)

    assert result["written"] is True
    assert result["conflict"] is True
    kinds = {Path(p).name.split(".")[1].split("-")[0] for p in result["snapshots"]}
    assert kinds == {"prev", "new"}, result["snapshots"]
    for snap in result["snapshots"]:
        assert Path(snap).exists()
    events = [e for e in analysis_merge.read_log(limit=20) if e.get("outcome") == "conflict"]
    assert events, "冲突必须留下结构化日志"
    assert events[-1]["current_digest"] != events[-1]["expected_digest"]
    _cleanup(question)


def test_lock_busy_never_overwrites(seeded):
    """拿不到短锁：不覆盖主文档，新内容留成快照并进 pending 队列。"""
    question = "锁竞争测试"
    _cleanup(question)
    _write(question=question, conclusion="第一份结论（res_eeeeeeeeee）", evidence=["res_eeeeeeeeee"])
    canonical = _canonical(question)
    before = canonical.read_text(encoding="utf-8")
    payload = _write_payload(question, conclusion="第二份结论")

    from app.cache import lock as lock_module

    with lock_module.short_lock(f"analysis:{payload['topic']}", wait_timeout=0):
        result = org_memory._write_document(payload)

    assert result["written"] is False and result["pending"] is True
    assert result["reason"] == "lock_busy"
    assert canonical.read_text(encoding="utf-8") == before, "拿不到锁时不允许改动主文档"
    assert analysis_merge.list_pending(), "未完成写入必须进 pending 队列"
    _cleanup(question)


def test_cas_exhaustion_parks_instead_of_overwriting(seeded, monkeypatch):
    """计算期间文件一直被改（CAS 反复失败）→ 放弃写回、保两份、进 pending。"""
    question = "CAS 用尽测试"
    _cleanup(question)
    payload = _write_payload(question)
    Path(payload["canonical"]).parent.mkdir(parents=True, exist_ok=True)
    calls = {"n": 0}

    def fake_digest(text):  # noqa: ANN001
        calls["n"] += 1
        return f"changed-{calls['n']}"

    monkeypatch.setattr(analysis_merge, "digest_text", fake_digest)
    result = org_memory._write_document(payload)

    assert result["written"] is False and result["pending"] is True
    assert result["reason"] == "cas_exhausted"
    _cleanup(question)


# ---------------------------------------------------------------- 短锁与 CAS 语义
def test_short_lock_is_short_and_excludes_locally():
    from app.cache import lock as lock_module

    order: list[str] = []

    def holder() -> None:
        with lock_module.short_lock("lock-probe", wait_timeout=0.5):
            order.append("A-in")
            import time as _time

            _time.sleep(0.05)
            order.append("A-out")

    thread = threading.Thread(target=holder)
    thread.start()
    import time as _time

    _time.sleep(0.01)
    with lock_module.short_lock("lock-probe", wait_timeout=0.01) as got:
        order.append(f"B-{'in' if got else 'busy'}")
    thread.join()
    assert order[0] == "A-in"
    assert "B-busy" in order, "同一 key 的第二个持有者必须拿不到锁（不排队阻塞）"


def test_atomic_write_leaves_no_partial_file(tmp_path):
    target = tmp_path / "doc.md"
    analysis_merge.atomic_write(target, "第一版")
    analysis_merge.atomic_write(target, "第二版")
    assert target.read_text(encoding="utf-8") == "第二版"
    leftovers = [p.name for p in tmp_path.iterdir() if ".tmp-" in p.name]
    assert leftovers == [], f"临时文件没有清理：{leftovers}"


def test_file_digest_tracks_content(tmp_path):
    path = tmp_path / "x.md"
    assert analysis_merge.file_digest(path) is None
    analysis_merge.atomic_write(path, "a")
    first = analysis_merge.file_digest(path)
    analysis_merge.atomic_write(path, "b")
    assert first != analysis_merge.file_digest(path)


# ---------------------------------------------------------------- LLM 摘要整合
class _StubLLM:
    def __init__(self, summary: str) -> None:
        self.summary = summary
        self.calls = 0

    async def invoke_json(self, system, user, schema, max_tokens=None):  # noqa: ANN001
        self.calls += 1
        return schema(summary=self.summary, conflicts=[])


def _doc_with_two_entries() -> analysis_merge.Doc:
    entries = [
        analysis_merge.Entry(analysis_id="a" * 32, as_of="2026-11-01T10:00:00",
                             conclusion="可用库存 420 件（res_1111111111）",
                             evidence=["res_1111111111"], key_metrics={"available": 420}),
        analysis_merge.Entry(analysis_id="b" * 32, as_of="2026-11-02T10:00:00",
                             conclusion="可用库存 100 件（res_2222222222）",
                             evidence=["res_2222222222"], key_metrics={"available": 100}),
    ]
    return analysis_merge.Doc(topic="t", department="男装", title="主题", entries=entries)


def test_reconcile_accepts_faithful_summary(monkeypatch):
    from app.llm import reset_llm

    doc = _doc_with_two_entries()
    stub = _StubLLM("两次分析：420 件（res_1111111111）/ 100 件（res_2222222222），口径冲突")
    monkeypatch.setattr("app.llm.get_llm", lambda: stub)
    reset_llm()
    summary, info = asyncio.run(analysis_merge.reconcile_summary(doc, use_llm=True))
    assert info["mode"] == "llm"
    assert "res_1111111111" in summary and "res_2222222222" in summary


def test_reconcile_rejects_summary_that_drops_evidence(monkeypatch):
    """LLM 只要漏掉原文数字/来源，就整段作废、回退确定性摘要 —— 不允许改写取证材料。"""
    from app.llm import reset_llm

    doc = _doc_with_two_entries()
    stub = _StubLLM("库存充足，无需补货")          # 数字与 result_id 全丢
    monkeypatch.setattr("app.llm.get_llm", lambda: stub)
    reset_llm()
    summary, info = asyncio.run(analysis_merge.reconcile_summary(doc, use_llm=True))
    assert info["mode"] == "deterministic"
    assert info["reason"] == "llm_summary_not_faithful"
    assert "res_1111111111" in summary              # 确定性摘要保留了来源


def test_reconcile_falls_back_when_llm_unavailable(monkeypatch):
    from app.llm import reset_llm

    class _Boom:
        async def invoke_json(self, *a, **k):  # noqa: ANN002, ANN003
            raise ConnectionError("connection reset")

    monkeypatch.setattr("app.llm.get_llm", lambda: _Boom())
    reset_llm()
    summary, info = asyncio.run(
        analysis_merge.reconcile_summary(_doc_with_two_entries(), use_llm=True))
    assert info["mode"] == "deterministic"
    assert "connection" in info["reason"].lower()
    assert summary


def test_keep_both_mode_does_not_call_llm(monkeypatch, _settings):
    from app.llm import reset_llm

    monkeypatch.setattr(_settings, "analysis_merge_mode", "keep-both")
    stub = _StubLLM("不该被调用")
    monkeypatch.setattr("app.llm.get_llm", lambda: stub)
    reset_llm()
    _, info = asyncio.run(
        analysis_merge.reconcile_summary(_doc_with_two_entries(), use_llm=False))
    assert info["mode"] == "deterministic"
    assert stub.calls == 0


def test_validate_render_rejects_missing_evidence():
    doc = _doc_with_two_entries()
    rendered = analysis_merge.render_doc(doc)
    analysis_merge.validate_render(rendered, doc)          # 正常渲染必须通过

    broken = rendered.replace("res_2222222222", "res_deadbeef00")
    with pytest.raises(analysis_merge.MergeRejected):
        analysis_merge.validate_render(broken, doc)


def test_pending_reconcile_is_drained(seeded, monkeypatch, _settings):
    """pending 队列里的 reconcile 任务会被 drain 应用（摘要写回主文档）。"""
    from app.llm import reset_llm

    question = "待合并队列测试"
    _cleanup(question)
    first = _write(question=question, conclusion="第一次：补 300 件（res_9999999999）",
                   evidence=["res_9999999999"])
    second = _write(question=question, conclusion="第二次：补 500 件（res_8888888888）",
                    evidence=["res_8888888888"])
    canonical = _canonical(question)
    payload = {"topic": org_memory.topic_key("男装", question), "department": "男装",
               "title": "待合并队列测试", "canonical": str(canonical),
               "expected_digest": analysis_merge.file_digest(canonical), "mode": "merge",
               "entry": _row_by_id(second["analysis_id"]).to_dict(),
               "at": "2026-11-12T00:00:00", "kind": "reconcile"}

    monkeypatch.setattr(_settings, "analysis_merge_mode", "merge")
    stub = _StubLLM("合并摘要：先后补 300 件（res_9999999999）与 500 件（res_8888888888）")
    monkeypatch.setattr("app.llm.get_llm", lambda: stub)
    reset_llm()
    analysis_merge.enqueue_pending(payload)

    drained = asyncio.run(org_memory.adrain_pending_merges(limit=5))

    assert drained["ok"] >= 1, drained
    text = canonical.read_text(encoding="utf-8")
    assert "合并摘要" in text
    assert first["analysis_id"] in text and second["analysis_id"] in text
    assert not analysis_merge.list_pending(), "处理成功的 pending 必须被清掉"
    _cleanup(question)


def test_rerender_restores_document_from_db(seeded):
    """markdown 只是投影：文件被破坏后能从 DB 行重建（取证材料不会丢）。"""
    question = "重渲染恢复测试"
    _cleanup(question)
    first = _write(question=question, conclusion="结论 A（res_abcdabcdab）",
                   evidence=["res_abcdabcdab"])
    second = _write(question=question, conclusion="结论 B（res_efefefefef）",
                    evidence=["res_efefefefef"])
    canonical = _canonical(question)
    analysis_merge.atomic_write(canonical, "被破坏了")

    result = org_memory.rerender_topic("男装", question)

    assert result["written"] is True and result["entries"] == 2
    text = canonical.read_text(encoding="utf-8")
    assert first["analysis_id"] in text and second["analysis_id"] in text
    _cleanup(question)


def test_unique_mode_never_merges(seeded, monkeypatch, _settings):
    monkeypatch.setattr(_settings, "analysis_merge_mode", "unique")
    question = "unique 模式测试"
    _cleanup(question)
    a = _write(question=question, conclusion="唯一模式 A")
    b = _write(question=question, conclusion="唯一模式 B")
    assert a["markdown_path"] != b["markdown_path"]
    assert Path(a["markdown_path"]).exists() and Path(b["markdown_path"]).exists()
    _cleanup(question)


def test_entries_union_never_drops_incoming_entry(seeded):
    """并集兜底：即使本次这条的 markdown_path 与 canonical 不一致，也不能漏写它。"""
    question = "并集兜底测试"
    _cleanup(question)
    _write(question=question, conclusion="已存在的结论（res_union00001）",
           evidence=["res_union00001"])
    payload = _write_payload(question, conclusion="本次新结论（res_union00002）")
    payload["entry"]["analysis_id"] = "d" * 32
    payload["entry"]["evidence"] = ["res_union00002"]

    result = org_memory._write_document(payload)

    assert result["written"] is True
    text = _canonical(question).read_text(encoding="utf-8")
    doc = analysis_merge.parse_doc(text)
    ids = doc.analysis_ids()
    assert "d" * 32 in ids, "本次这条被漏写了"
    assert len(ids) == 2
    assert "res_union00002" in text
    _cleanup(question)


def test_save_analysis_reports_document_status_honestly(seeded):
    """文件在事务提交后才写：返回值必须诚实标注 pending_commit，而不是 document_written=False。"""
    question = "document_status 测试"
    _cleanup(question)
    saved = _write(question=question)
    assert saved["document_status"] == "pending_commit"
    assert "document_written" not in saved
    assert Path(saved["markdown_path"]).exists()     # 提交之后文件确实写好了
    _cleanup(question)


def test_pending_merges_give_up_after_max_attempts(seeded, monkeypatch):
    """同一条 pending 无限重试会空转：超过次数上限要放弃并留痕。"""
    question = "无限重试测试"
    _cleanup(question)
    saved = _write(question=question)                    # 先有一条真实行（否则会被判定为 stale）
    payload = _write_payload(question)
    payload.update({
        "kind": "write", "attempts": 4,
        "entry": _row_by_id(saved["analysis_id"]).to_dict(),
    })
    analysis_merge.enqueue_pending(payload)

    monkeypatch.setattr(org_memory, "_write_document",
                        lambda p: {"written": False, "pending": True, "reason": "lock_busy"})
    drained = asyncio.run(org_memory.adrain_pending_merges(limit=5))

    assert drained["failed"] >= 1
    assert not analysis_merge.list_pending(), "放弃后必须从队列里移除"
    events = [e for e in analysis_merge.read_log(limit=30)
              if e.get("outcome") == "merge_abandoned"]
    assert events, "放弃必须留痕（merge_abandoned）"
    _cleanup(question)


# ---------------------------------------------------------------- 辅助
def _write_payload(question: str, conclusion: str = "并发结论") -> dict:
    """构造 _write_document 需要的最小 payload（模拟「读时已取哈希」之后的状态）。"""
    canonical = _canonical(question)
    entry = analysis_merge.Entry(
        analysis_id="c" * 32, as_of="2026-11-12T10:00:00", title=question, question=question,
        conclusion=conclusion, key_metrics={"available": 1}, evidence=["res_7777777777"],
        metric_version="2026Q1",
    )
    return {
        "topic": org_memory.topic_key("男装", question), "department": "男装", "title": question,
        "canonical": str(canonical), "expected_digest": analysis_merge.file_digest(canonical),
        "entry": entry.to_dict(), "mode": "keep-both", "at": "2026-11-12T10:00:00",
    }


def _row_by_id(analysis_id: str) -> analysis_merge.Entry:
    """在 session 内转成 Entry（ORM 实例离开 session 会 Detached）。"""
    with session_scope() as session:
        row = session.query(AnalysisResult).filter_by(analysis_id=analysis_id).one()
        return analysis_merge.Entry.from_row(row)
