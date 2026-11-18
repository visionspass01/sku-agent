"""共享账号表 `user` 的建表入口。

拆成独立小模块是为了打破循环导入：`app.core.accounts` 需要建表，
而 `app.db.models` 里定义 `AppUser`（models 不 import accounts）。
"""

from __future__ import annotations


def ensure_user_table() -> bool:
    """幂等创建 `user` 表（只建这一张，不碰其它表）；返回是否执行了建表。"""
    from app.db.engine import get_engine
    from app.db.models import AppUser

    table = AppUser.__table__
    try:
        table.create(get_engine(), checkfirst=True)
        return True
    except Exception:  # noqa: BLE001 —— 表已存在/并发建表都视为成功
        return False


__all__ = ["ensure_user_table"]
