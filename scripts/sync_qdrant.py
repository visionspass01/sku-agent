"""把 SQLite 里的知识库语料全量重灌进向量库（Qdrant）。

什么时候用：
- 首次启用 Qdrant；
- 换了 embedding 模型/维度（0.6b 1024 ⊃ 4b 2560）→ 集合会被自动重建；
- 怀疑向量库与 SQLite 不一致（`/api/health.vector_store.points` 与 `kb_chunk` 数不符）。

用法：
    python scripts/sync_qdrant.py            # 用当前 .env 的 QDRANT_URL / EMBEDDING_BACKEND
    python scripts/sync_qdrant.py --status   # 只看状态，不写
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from app.db.engine import session_scope  # noqa: E402
from app.db.models import KbChunk  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="向量库全量同步（Qdrant）")
    parser.add_argument("--status", action="store_true", help="只打印状态，不写入")
    args = parser.parse_args()

    from app.config import get_settings
    from app.rag import vector_store as vs
    from app.rag.index import sync_store

    settings = get_settings()
    store = vs.get_store()
    with session_scope() as session:
        expected = int(session.query(KbChunk).count())
        if store is None:
            print(f"向量库未启用：VECTOR_STORE={settings.vector_store} QDRANT_URL={settings.qdrant_url or '(空)'}")
            print(f"SQLite 语料：{expected} 块（检索走内存模式）")
            return 0
        status = store.health()
        print(f"Qdrant：{status.get('mode')} {status.get('url')} collection={status.get('collection')} "
              f"dim={status.get('dim')} points={status.get('points')}")
        print(f"SQLite 语料：{expected} 块")
        if args.status:
            return 0 if status.get("points") == expected else 2
        print(f"embedding：{settings.embedding_backend} / {settings.tencent_embedding_model}")
        result = sync_store(session)
        print("同步完成：" + json.dumps(result, ensure_ascii=False))
        after = store.count()
        print(f"重建后点数：{after}（SQLite {expected}）")
        return 0 if after == expected else 1


if __name__ == "__main__":
    sys.exit(main())
