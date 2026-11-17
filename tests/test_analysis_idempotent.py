"""P0-2：档案沉淀幂等（同结论只保留一份档案，重试/重复点击不产生重复）。"""

from __future__ import annotations

from sqlalchemy import func, select

from app.db.engine import session_scope
from app.db.models import AnalysisResult
from app.memory import org_memory


def _count() -> int:
    with session_scope() as session:
        return int(session.scalar(select(func.count()).select_from(AnalysisResult)) or 0)


def test_same_content_is_deduped(seeded):
    before = _count()
    kwargs = dict(
        department="男装", username="admin", question="P0-2 幂等",
        conclusion="同一结论只保留一份", evidence=["res_1111111111"],
    )
    first = org_memory.save_analysis(**kwargs)
    second = org_memory.save_analysis(**kwargs)

    assert first.get("deduped") is False
    assert second.get("deduped") is True
    assert second["analysis_id"] == first["analysis_id"]
    assert _count() == before + 1, "重复沉淀不应新增档案行"


def test_different_conclusion_creates_new_archive(seeded):
    base = dict(department="男装", username="admin", question="P0-2 变体", evidence=["res_2222222222"])
    a = org_memory.save_analysis(conclusion="结论 A", **base)
    b = org_memory.save_analysis(conclusion="结论 B", **base)
    assert a["analysis_id"] != b["analysis_id"]
    assert b.get("deduped") is False


def test_different_department_not_deduped(seeded):
    common = dict(username="admin", question="P0-2 跨部门", conclusion="同一句话",
                  evidence=["res_3333333333"])
    a = org_memory.save_analysis(department="男装", **common)
    b = org_memory.save_analysis(department="女装", **common)
    assert a["analysis_id"] != b["analysis_id"], "不同部门的档案不应互相判重"


def test_legacy_db_migration_adds_hash_column(tmp_path):
    """老库（无 content_hash 列/索引）启动时自动补列与索引。"""
    from sqlalchemy import create_engine, inspect, text

    from app.db.engine import ensure_columns

    engine = create_engine(f"sqlite:///{(tmp_path / 'legacy_app.db').as_posix()}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE analysis_result ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, analysis_id VARCHAR(36), title VARCHAR(128), "
            "department VARCHAR(16), username VARCHAR(32), session_id VARCHAR(36), "
            "plan_id VARCHAR(64), source VARCHAR(16), question TEXT, conclusion TEXT, "
            "key_metrics JSON, evidence JSON, sql_fingerprints JSON, metric_version VARCHAR(32), "
            "markdown_path VARCHAR(256), created_at DATETIME)"
        ))
    added = ensure_columns(engine)
    assert "analysis_result.content_hash" in added
    cols = {c["name"] for c in inspect(engine).get_columns("analysis_result")}
    assert "content_hash" in cols
    idx = {i.get("name") for i in inspect(engine).get_indexes("analysis_result")}
    assert "idx_analysis_hash" in idx
    assert ensure_columns(engine) == []  # 幂等
