"""RAG 规模基准：1200 篇文档下「能不能撑住」（评审 3.1 的可验证部分）。

为什么需要单独一个基准：14 篇 SOP 上跑出漂亮的 R@1 说明不了任何事——换到 1200 篇
（真实企业知识库的保守量级）时，索引构建、内存占用、单查询延迟、无答案误判都会变。
本脚本用**确定性合成语料**造 1200 篇（同一批 SOP 模板 + 变体 + 干扰文档），在临时库里
建索引并跑题集，输出可复现的四项指标：

- 索引构建时间（含分块 + 向量化 + 写库）
- 单查询 P50 / P95 延迟（含融合与可选重排）
- 检索质量（R@1 / R@5 / MRR）+ 无答案命中率
- 内存占用（块数 × 向量维度）

用法：
    python scripts/bench_rag_scale.py                       # 默认 1200 篇
    python scripts/bench_rag_scale.py --docs 1200 --query 100 --rerank llm
    python scripts/bench_rag_scale.py --docs 300 --json data/eval/scale.json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import random
import re
import statistics
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

KB_DIR = ROOT / "data" / "kb"

# 干扰文档：与业务无关的行业资料，用来稀释词法信号（真实知识库里绝大多数文档是噪声）
NOISE_TEMPLATES = (
    "电商运营周报模板：本周 GMV {n} 万，环比 {p}%，主要由{cat}类目贡献。",
    "竞品分析纪要：{cat}类目下 TOP{n} 店铺的价格带集中在 {p} 元区间。",
    "客服话术：关于{p}天无理由退货的沟通要点与常见问题答复。",
    "仓储操作规范：入库验收需在 {n} 小时内完成，异常件单独存放。",
    "物流时效说明：{cat} 区域标准时效 {n} 天，偏远地区顺延。",
    "直播脚本片段：第 {n} 分钟进入福利款，主播需要提示库存与尺码。",
)
CATEGORIES = ("女装", "男装", "童装", "鞋包", "家居")
SEASONS = ("春夏", "秋冬", "四季")


def build_corpus(target: int, seed: int = 7) -> list[tuple[str, str]]:
    """生成 (doc_id, markdown 文本) 列表：真实 SOP 做变体 + 合成干扰文档（约 60%）。

    语料构成（审计修正）：变体只填 `target - noise_target` 篇，剩下的位置留给干扰文档，
    最后不再截断——旧实现先填满 `target` 篇变体、再追加噪声、再 `corpus[:target]`
    把噪声整段裁掉，等于"没有任何干扰文档"，与本文档与 README 的描述不符。
    """
    rng = random.Random(seed)
    base_docs = sorted(KB_DIR.glob("*.md"))
    sources = [(p.stem, p.read_text(encoding="utf-8")) for p in base_docs]
    noise_target = int(target * 0.6)
    variant_target = max(1, target - noise_target)
    corpus: list[tuple[str, str]] = []
    idx = 0
    while len(corpus) < variant_target:
        source_id, text = sources[idx % len(sources)]
        variant = idx // len(sources)
        if variant == 0:
            corpus.append((source_id, text))
        else:
            body = text
            # 变体：改 doc_id/部门/季节/版本，让标题前缀与元数据都不同（模拟不同部门的同类 SOP）
            cat = rng.choice(CATEGORIES)
            season = rng.choice(SEASONS)
            body = re.sub(r"^doc_id: .*$", f"doc_id: {source_id}-v{variant}", body,
                          count=1, flags=re.MULTILINE)
            body = body.replace("department: 通用", f"department: {cat}")
            body = body.replace("season: 全部", f"season: {season}")
            body = re.sub(r"^version: .*$", f"version: v{1 + variant}", body,
                          count=1, flags=re.MULTILINE)
            corpus.append((f"{source_id}-v{variant}", body))
        idx += 1
    # 灌入干扰文档（约占 60%，模拟真实库里大量无关资料）
    for i in range(noise_target):
        template = NOISE_TEMPLATES[i % len(NOISE_TEMPLATES)]
        body = template.format(n=10 + i % 90, p=3 + i % 40, cat=CATEGORIES[i % len(CATEGORIES)])
        text = (
            "---\n"
            f"doc_id: noise-{i:04d}\n"
            f"title: {CATEGORIES[i % len(CATEGORIES)]}运营资料 {i:04d}\n"
            "department: 通用\ncategory: 全部\nseason: 全部\nwarehouse: 全部\n"
            "platform: 全部\nversion: v1\n---\n\n"
            f"# {CATEGORIES[i % len(CATEGORIES)]}运营资料 {i:04d}\n\n"
            f"## 要点\n{body}\n\n## 备注\n本条为规模基准用的合成干扰文档，不承载业务规则。\n"
        )
        corpus.append((f"noise-{i:04d}", text))
    return corpus[:max(1, target)]


def _write_corpus(corpus: list[tuple[str, str]], target_dir: pathlib.Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    for doc_id, text in corpus:
        (target_dir / f"{doc_id}.md").write_text(text, encoding="utf-8")


def _setup_db(tmp: pathlib.Path):  # noqa: ANN201
    from app.config import Settings, set_settings
    from app.db.engine import init_schema, make_engine, set_engine

    settings = Settings(
        database_url=f"sqlite:///{(tmp / 'scale.db').as_posix()}",
        redis_url="",
        mcp_transport="direct",
        scheduler_enabled=False,
        analysis_merge_mode="keep-both",
        department_mapping_cache=False,
        catalog_sync_on_startup=False,
        grounding_enabled=False,
    )
    settings.analyses_dir = str(tmp / "analyses")
    set_settings(settings)
    engine = make_engine(settings)
    set_engine(engine)
    init_schema(engine)
    return settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RAG 1200 篇规模基准")
    parser.add_argument("--docs", type=int, default=1200)
    parser.add_argument("--query", type=int, default=60, help="用题集里的前 N 条做延迟采样")
    parser.add_argument("--rerank", default="none", choices=["none", "heuristic", "llm", "cross"])
    parser.add_argument("--json", default="", help="把结果写成 json")
    args = parser.parse_args(argv)

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="bench_rag_"))
    _setup_db(tmp)
    kb_dir = tmp / "kb"
    corpus = build_corpus(args.docs)
    _write_corpus(corpus, kb_dir)

    from sqlalchemy import func, select

    from app.db.engine import session_scope
    from app.db.models import KbChunk, KbDoc
    from app.rag.eval import evaluate, load_golden, load_negatives, retriever_search
    from app.rag.hybrid import get_retriever, invalidate_retriever
    from app.rag.index import build_index

    started = time.perf_counter()
    with session_scope() as session:
        stats = build_index(session, kb_dir)
    build_ms = int((time.perf_counter() - started) * 1000)
    invalidate_retriever()

    with session_scope() as session:
        docs = int(session.scalar(select(func.count()).select_from(KbDoc)) or 0)
        chunks = int(session.scalar(select(func.count()).select_from(KbChunk)) or 0)
        load_started = time.perf_counter()
        get_retriever(session)          # 触发装载（单例）
        load_ms = int((time.perf_counter() - load_started) * 1000)

        # 延迟采样（含融合；重排可选）
        search = retriever_search(session, 5, mode="rrf", rerank_backend=args.rerank)
        golden = load_golden()[: max(1, args.query)]
        latencies: list[float] = []
        for row in golden:
            t0 = time.perf_counter()
            search(row["query"], 5, rewritten=None)
            latencies.append((time.perf_counter() - t0) * 1000)

        rows = list(load_golden()) + list(load_negatives())
        search_all = retriever_search(session, 5, mode="rrf", rerank_backend=args.rerank)
        report = evaluate(rows, search_all, topk=5)

    latencies.sort()
    p50 = round(statistics.median(latencies), 1) if latencies else 0.0
    p95 = round(latencies[int(0.95 * (len(latencies) - 1))], 1) if latencies else 0.0
    vectors_kb = round(chunks * 512 * 4 / 1024, 1)      # 512 维 float32
    summary = {
        "documents": docs,
        "chunks": chunks,
        "avg_chunks_per_doc": round(chunks / docs, 1) if docs else 0.0,
        "build_ms": build_ms,
        "retriever_load_ms": load_ms,
        "query_p50_ms": p50,
        "query_p95_ms": p95,
        "recall@1": report["recall_at"]["recall@1"],
        "recall@5": report["recall_at"]["recall@5"],
        "mrr": report["mrr"],
        "ndcg@5": report["ndcg@5"],
        "no_answer_rate": report.get("no_answer_rate"),
        "memory_vectors_kb": vectors_kb,
        "rerank": args.rerank,
        "rerank_degraded": getattr(search_all, "degraded", 0),
        "index_stats": stats.to_dict(),
    }

    print(f"语料：{docs} 篇 / {chunks} 块（平均 {summary['avg_chunks_per_doc']} 块/篇）")
    print(f"索引构建：{build_ms} ms（新增文档 {len(stats.added)} 篇，块 +{stats.chunks_added}"
          f"/-{stats.chunks_removed}）")
    print(f"检索器装载：{load_ms} ms；向量内存约 {vectors_kb} KB")
    print(f"单查询延迟：P50 {p50} ms / P95 {p95} ms（n={len(latencies)}，重排={args.rerank}）")
    rate = summary["no_answer_rate"]
    rate_text = "-" if rate is None else f"{rate:.0%}"
    print(f"质量：R@1 {summary['recall@1']:.1%} · R@5 {summary['recall@5']:.1%} · "
          f"MRR {summary['mrr']:.3f} · 无答案命中率 {rate_text}")
    if summary["rerank_degraded"]:
        print(f"注意：重排降级 {summary['rerank_degraded']} 次（该后端离线不可用，回退融合序）")

    ok = True
    if p95 > 250 and args.rerank == "none":
        print(f"[WARN] P95 {p95}ms 超过 250ms 目标")
        ok = False
    if summary["recall@1"] < 0.85:
        print(f"[WARN] R@1 {summary['recall@1']:.1%} 低于 0.85 目标")
        ok = False
    if args.json:
        pathlib.Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(args.json).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已写入 {args.json}")
    print("[OK] 达到规模目标" if ok else "[FAIL] 未达到规模目标")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
