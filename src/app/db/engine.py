"""数据库连接与 schema 初始化。"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import Engine, create_engine, event

from app.config import Settings, get_settings
from app.db.models import Base

_engine: Engine | None = None

# SQLite 写性能：默认 journal_mode=delete + synchronous=FULL 时，本机 24MB 演示库
# 单次 commit 实测要 400~1000ms（一次工具调用要写一条取数台账，直接拖慢对话）；
# 改成 WAL + synchronous=NORMAL 后单次 commit 降到 1~2ms。
_SQLITE_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA temp_store=MEMORY",
)


def make_engine(settings: Settings | None = None) -> Engine:
    settings = settings or get_settings()
    url = settings.database_url
    kwargs: dict = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        # 本地演示：单文件 + 多线程访问（uvicorn worker / 后台线程）
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _record):  # noqa: ANN001
            cursor = dbapi_conn.cursor()
            for pragma in _SQLITE_PRAGMAS:
                try:
                    cursor.execute(pragma)
                except Exception:  # noqa: BLE001 —— 只读副本等场景下个别 PRAGMA 可能失败
                    pass
            cursor.close()

    return engine


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


def set_engine(engine: Engine) -> None:
    """测试/脚本替换引擎。"""
    global _engine
    _engine = engine


def init_schema(engine: Engine | None = None) -> None:
    eng = engine or get_engine()
    Base.metadata.create_all(eng)
    ensure_columns(eng)


# 既有演示库补列（create_all 不会 ALTER 已存在的表）：表名 -> [(列名, DDL 类型)]
_EXTRA_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "after_sales_ticket": [
        ("approver", "VARCHAR(32) NULL"),
        ("decided_at", "DATETIME NULL"),
        ("note", "VARCHAR(256) NULL"),
    ],
    "analysis_result": [
        ("content_hash", "VARCHAR(64) NULL"),
    ],
    "auth_event": [
        # 失败登录留痕：老库没有这一列，登录时会因为 INSERT 缺列直接 500
        ("result", "VARCHAR(16) DEFAULT 'ok'"),
    ],
    # 单轮运行详情补「聚合维度」列：per-agent 看板/成本归因/降级追踪靠它们做 GROUP BY，
    # 老库（演示库）没有这些列，不补的话新代码写入会直接报 no such column。
    "chat_turn_log": [
        ("agent_id", "VARCHAR(32) DEFAULT ''"),
        ("agent_version", "VARCHAR(16) DEFAULT ''"),
        ("model", "VARCHAR(64) DEFAULT ''"),
        ("tokens_in", "INTEGER DEFAULT 0"),
        ("tokens_out", "INTEGER DEFAULT 0"),
        ("cost_cny", "FLOAT DEFAULT 0"),
        ("tool_calls", "INTEGER DEFAULT 0"),
        ("grounding_status", "VARCHAR(16) DEFAULT ''"),
        ("fallback_used", "BOOLEAN DEFAULT 0"),
    ],
    # 知识库块集合指纹：老库没有，不补的话「只重建变动文档」的对账逻辑会报 no such column
    "kb_doc": [
        ("chunk_count", "INTEGER DEFAULT 0"),
        ("chunks_sha", "VARCHAR(64) DEFAULT ''"),
        ("chunker_version", "VARCHAR(8) DEFAULT ''"),
    ],
}


def _analysis_duplicate_count(eng: Engine) -> int:
    """同一内容指纹出现多行的组数（决定能否把普通索引升级为唯一索引）。"""
    from sqlalchemy import text

    try:
        with eng.connect() as conn:
            value = conn.execute(text(
                "SELECT COUNT(*) FROM ("
                " SELECT content_hash FROM analysis_result"
                " WHERE content_hash IS NOT NULL"
                " GROUP BY content_hash HAVING COUNT(*) > 1"
                ") dup"
            )).scalar()
        return int(value or 0)
    except Exception:  # noqa: BLE001 —— 表结构不认识时不要阻断启动
        return 0


def analysis_dedupe_status(engine: Engine | None = None) -> dict:
    """档案去重的物理保障状态：唯一索引是否就位、还有多少组历史重复。

    没有唯一索引时，并发写同一条结论会在「查不到 → 都插入」之间产生重复行，
    唯一索引 + `IntegrityError` 捕获才是真正安全的；历史重复数据得先清理（见
    `scripts/dedupe_analyses.py`），所以这里可能长期处于 degraded。
    """
    from sqlalchemy import inspect

    eng = engine or get_engine()
    try:
        indexes = {i.get("name") for i in inspect(eng).get_indexes("analysis_result")}
    except Exception:  # noqa: BLE001
        return {"unique_index": False, "duplicates": 0, "degraded": False, "error": "unavailable"}
    unique = "uq_analysis_hash" in indexes
    duplicates = 0 if unique else _analysis_duplicate_count(eng)
    return {"unique_index": unique, "duplicates": duplicates,
            "degraded": (not unique) and duplicates > 0,
            "note": ("唯一索引已就位：并发写同一结论不会再产生重复行" if unique
                     else "暂无唯一索引（历史重复未清理）；并发写同结论可能产生重复行，"
                          "可跑 scripts/dedupe_analyses.py 后重启自动升级")}


def ensure_columns(engine: Engine | None = None) -> list[str]:
    """轻量迁移：给旧版本数据文件补上新增列（幂等，SQLite / MySQL 通用）。"""
    from sqlalchemy import inspect, text

    eng = engine or get_engine()
    added: list[str] = []
    insp = inspect(eng)
    tables = set(insp.get_table_names())
    for table, columns in _EXTRA_COLUMNS.items():
        if table not in tables:
            continue
        existing = {c["name"] for c in insp.get_columns(table)}
        for name, ddl in columns:
            if name in existing:
                continue
            with eng.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
            added.append(f"{table}.{name}")

    # 内容指纹索引同样要补（老库建表时没有它）；用普通索引，允许历史重复数据
    if "analysis_result" in tables:
        existing_idx = {i.get("name") for i in insp.get_indexes("analysis_result")}
        if "idx_analysis_hash" not in existing_idx:
            with eng.begin() as conn:
                if eng.dialect.name == "mysql":
                    conn.execute(text(
                        "CREATE INDEX idx_analysis_hash ON analysis_result (content_hash)"
                    ))
                else:
                    conn.execute(text(
                        "CREATE INDEX IF NOT EXISTS idx_analysis_hash "
                        "ON analysis_result (content_hash)"
                    ))
            added.append("analysis_result.idx_analysis_hash")
        # 历史重复清理干净后，把索引升级为唯一：并发写同结论才真正安全（幂等，重复跑无害）
        if "uq_analysis_hash" not in existing_idx and _analysis_duplicate_count(eng) == 0:
            try:
                with eng.begin() as conn:
                    if eng.dialect.name == "mysql":
                        conn.execute(text(
                            "CREATE UNIQUE INDEX uq_analysis_hash ON analysis_result (content_hash)"
                        ))
                    else:
                        conn.execute(text(
                            "CREATE UNIQUE INDEX IF NOT EXISTS uq_analysis_hash "
                            "ON analysis_result (content_hash)"
                        ))
                added.append("analysis_result.uq_analysis_hash")
            except Exception:  # noqa: BLE001 —— 并发/权限原因建不上就保持普通索引
                pass
    return added


def drop_schema(engine: Engine | None = None) -> None:
    Base.metadata.drop_all(engine or get_engine())


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator:
    from sqlalchemy.orm import Session

    eng = engine or get_engine()
    session = Session(eng)
    try:
        yield session
        session.commit()
        # 提交成功后才执行「提交后动作」（例如把档案写进 markdown 文件）：
        # 事务回滚时不会留下孤儿文件，文件内容也不会引用未提交的行。
        _run_after_commit(session)
    except Exception:
        session.rollback()
        session.info.pop("after_commit", None)
        raise
    finally:
        session.close()


def _run_after_commit(session) -> None:  # noqa: ANN001 —— sqlalchemy Session
    """执行 `session.info["after_commit"]` 里登记的回调（每个只执行一次）。"""
    import logging

    callbacks = session.info.pop("after_commit", []) or []
    for callback in callbacks:
        try:
            callback()
        except Exception as exc:  # noqa: BLE001 —— 提交已经成功，回调失败只告警
            logging.getLogger("sku-agent.db").warning("after_commit 回调失败：%s", exc)


def register_after_commit(session, callback) -> None:  # noqa: ANN001
    """登记「本事务提交成功后」要做的事（commit 由 session_scope 负责时使用）。"""
    session.info.setdefault("after_commit", []).append(callback)
