"""巡检执行器：单 SKU 全链路研判 + 批量巡检（Stream 消费、幂等去重、分布式锁、实时进度、可终止）。"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
import uuid

from sqlalchemy import select

from app.agents.checkpoint import inspection_graph, prune_threads, touch_thread
from app.cache.lock import LockTimeoutError, redis_lock
from app.cache.stream_queue import get_stream_queue
from app.config import categories_for, department_of, get_settings
from app.db import repos
from app.db.engine import session_scope
from app.db.models import InspectionResult, InspectionRun, PatrolLog, Sku
from app.llm import department_context
from app.pipeline import progress as patrol_progress
from app.pipeline.screening import screen

logger = logging.getLogger("sku-agent.patrol")

MAX_PATROL_SKUS = 200


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _persist_result(run_id: str, result: dict) -> None:
    final = result.get("final_state") or {}
    data = final.get("data") or {}
    erp = data.get("erp") or {}
    judgment = final.get("judgment") or {}
    replenish = final.get("replenish") or {}
    rag = data.get("rag") or {}
    plan = final.get("plan") or {}
    verification = final.get("verification") or {}

    data_bundle = {
        "sku": erp.get("sku") or {},
        "features": erp.get("features") or {},
        "judgment": judgment,
        "replenish": replenish,
        "market": data.get("market") or {},
        "rag_citations": rag.get("citations") or [],
        "trace": final.get("trace") or [],
    }
    citations = [
        {"chunk_id": c, "doc_id": c.split("#")[0]} for c in (plan.get("citations") or [])
    ]
    with session_scope() as session:
        repos.add_result(
            session,
            run_id=run_id,
            sku_id=final.get("sku_id") or result.get("sku_id", ""),
            risk_level=str(plan.get("risk_level") or "中"),
            plan=plan,
            citations=citations,
            verifier=verification,
            data_bundle=data_bundle,
            degraded=bool(final.get("degraded") or (final.get("status") == "degraded")),
            elapsed_ms=int(result.get("elapsed_ms") or 0),
        )


def _screen_and_filter(mode: str, department: str | None, *, run_screen: bool = True
                       ) -> tuple[list, dict, int, int]:
    """同步执行量化筛查 + 部门过滤 + 模式去重（由 run_patrol 放进线程池调用）。

    返回 (待处理异常列表, sku→品类映射, 筛查命中数, 因最近 1h 已处理而跳过的数量)。

    `run_screen=False`：另一个实例正在做筛查（抢不到 `lock:screen:<小时>`），
    本轮直接复用库里上一次的异常列表 —— 避免两个巡检同时 `DELETE + INSERT` 同一小时的
    anomalies（那会互相删掉对方刚插入的行，静默产生重复或丢失）。
    """
    with session_scope() as session:
        anomalies = screen(session) if run_screen else repos.list_anomalies(session, limit=500)
        sku_cat = {
            s.sku_id: s.category
            for s in session.query(Sku).filter(
                Sku.sku_id.in_([a.sku_id for a in anomalies])
            )
        } if anomalies else {}
        if department:
            cats = set(categories_for(department) or [])
            anomalies = [a for a in anomalies if sku_cat.get(a.sku_id) in cats]

        screened_total = len(anomalies)  # 全量筛查命中（部门过滤后、模式过滤前）
        skipped_recent = 0

        if mode == "hourly":
            cutoff = dt.datetime.now() - dt.timedelta(minutes=60)
            recent = {
                row[0] for row in session.execute(
                    select(InspectionResult.sku_id)
                    .join(InspectionRun, InspectionResult.run_id == InspectionRun.id)
                    .where(InspectionRun.started_at >= cutoff)
                )
            }
            before = len(anomalies)
            anomalies = [a for a in anomalies if a.sku_id not in recent]
            skipped_recent = before - len(anomalies)
    return anomalies, sku_cat, screened_total, skipped_recent


def _merge_updates(final: dict, update: dict | None) -> None:
    """把 LangGraph updates 流式结果合并回最终 state（与 reducer 语义对齐）。"""
    for key, value in (update or {}).items():
        if key == "data":
            final.setdefault("data", {})
            final["data"].update(value or {})
        elif key == "trace":
            final.setdefault("trace", [])
            final["trace"].extend(value or [])
        else:
            final[key] = value


async def _invoke_graph(sku_id: str, run_id: str, as_of: str | None, on_event=None,
                        thread_id: str | None = None) -> dict:
    """执行主图；on_event 提供时使用 updates 流，便于展示当前链路节点。

    thread_id 决定 checkpointer 线程：同一 SKU 固定 `sku:<sku_id>`，
    追问（followup）据此复用上一轮 full state，不再重复调用 MCP 取数。
    """
    settings = get_settings()
    init = {
        "sku_id": sku_id,
        "run_id": run_id,
        "as_of": as_of or dt.date.today().isoformat(),
        "attempts": 0,
        "degraded": False,
        "data": {},
        "trace": [],
    }
    config = {
        "max_concurrency": settings.patrol_concurrency,
        "configurable": {"thread_id": thread_id or f"sku:{sku_id}"},
    }
    async with inspection_graph() as graph:
        # 每次「重新研判」都清空旧线程：避免 checkpointer 把上一轮的 attempts/verification
        # 带进来，导致重跑时校验循环次数被历史占用（追问走 followup_sku，不复用这里）。
        try:
            await graph.checkpointer.adelete_thread(config["configurable"]["thread_id"])
        except Exception:  # noqa: BLE001
            pass
        if on_event is None:
            return await graph.ainvoke(init, config=config)

        final: dict = dict(init)
        last_ts = time.monotonic()
        async for chunk in graph.astream(init, config=config, stream_mode="updates"):
            for node, update in (chunk or {}).items():
                if not isinstance(update, dict) or node.startswith("__"):
                    continue
                ms = int((time.monotonic() - last_ts) * 1000)
                last_ts = time.monotonic()
                _merge_updates(final, update)
                on_event(node, ms, update)
        return final


class InspectTimeoutError(RuntimeError):
    """单 SKU 研判超过 `INSPECT_TIMEOUT_SECONDS`（硬上限，防止一次研判无限占住并发槽与锁）。

    为什么必须有（复审 P1-2 的同源问题）：图的校验循环最多 `MAX_REVISIONS+1` 轮，
    每轮 5~6 次 LLM 调用，单次上限 120s —— 没有任何时间上限时，最坏情况是**几十分钟**，
    而顺序锁 TTL 只有 `lock_ttl_ms`（120s）。锁一过期，另一个副本就能对同一 SKU 再跑一遍
    （双倍 token + 重复写结果）。所以：给研判一个硬上限，并让锁 TTL 从该上限派生。
    """


def _inspect_lock_ttl_ms(settings) -> int:  # noqa: ANN001 —— app.config.Settings
    """顺序锁 TTL = max(配置下限, 研判硬上限 + 60s 余量)。

    `lock_ttl_ms` 只作为"下限"保留（历史配置仍生效），真正决定 TTL 的是研判预算，
    这样"改大 LLM 超时/校验轮次"时锁不会悄悄变短。
    """
    timeout = float(getattr(settings, "inspect_timeout_seconds", 0.0) or 0.0)
    if timeout <= 0:                       # 0 = 不设硬上限，退回配置下限
        return int(settings.lock_ttl_ms)
    return max(int(settings.lock_ttl_ms), int((timeout + 60.0) * 1000))


async def _invoke_graph_with_timeout(sku_id: str, run_id: str, as_of: str | None,
                                     on_event=None, thread_id: str | None = None) -> dict:
    """带硬超时的图执行；超时抛 `InspectTimeoutError`（锁由上层 `async with` 负责释放）。"""
    settings = get_settings()
    timeout = float(getattr(settings, "inspect_timeout_seconds", 0.0) or 0.0)
    runner = _invoke_graph(sku_id, run_id, as_of, on_event=on_event, thread_id=thread_id)
    if timeout <= 0:
        return await runner
    try:
        return await asyncio.wait_for(runner, timeout=timeout)
    except asyncio.TimeoutError as exc:
        logger.warning("单 SKU 研判超时（>%.0fs）：sku=%s run=%s", timeout, sku_id, run_id)
        raise InspectTimeoutError(f"研判超时（>{timeout:.0f}s），已中止本轮") from exc


async def inspect_sku(sku_id: str, run_id: str, as_of: str | None = None, on_event=None,
                      thread_id: str | None = None) -> dict:
    """执行单个 SKU 的全链路研判；调用方负责 inspection_run 的创建与状态收尾。

    LLM 调用会按该 SKU 所属部门归因；on_event(node, ms, update) 用于进度展示。
    thread_id 落进 inspection_run.params，前端据此发起「追问」（followup）。
    """
    settings = get_settings()
    started = time.monotonic()
    status = "failed"
    error: str | None = None
    final_state: dict = {}
    sku_department: str | None = None
    tid = thread_id or f"sku:{sku_id}"

    def _load_sku_scope() -> str | None:
        with session_scope() as session:
            sku = repos.get_sku(session, sku_id)
            repos.update_run_params(session, run_id, thread_id=tid)
            return department_of(sku.category) if sku is not None else None

    # 同步 ORM 会话丢线程池（评审 v3-①）：研判本身要调 LLM 几十秒，这里再塞几毫秒的
    # 块级 DB 完全没必要，但 SSE 心跳靠的就是事件循环不被占住。
    sku_department = await asyncio.to_thread(_load_sku_scope)
    # 复审 F2：**开始就跑一次 touch**。早先只在结束时登记线程活跃时间，长研判（最多
    # INSPECT_TIMEOUT_SECONDS）跑的过程中带着上一轮的旧时间，可能被并发的
    # `prune_threads()` 当成"最旧线程"删掉，后续追问就找不到这轮的采集数据。
    try:
        await touch_thread(tid)
    except Exception:  # noqa: BLE001 —— 登记失败不影响研判
        pass
    try:
        with department_context(sku_department):
            # 顺序锁是真异步的（redis.asyncio + asyncio.sleep 轮询）：
            # 同一 SKU 重复研判直接 409，等锁期间不会阻塞事件循环。
            # TTL 从研判硬上限派生（见 `_inspect_lock_ttl_ms`）：锁必须比"最坏研判"活得久，
            # 否则多副本下会出现同一 SKU 被并发研判（复审 P1-2 同源）。
            async with redis_lock(f"lock:inspect:{sku_id}",
                                  ttl_ms=_inspect_lock_ttl_ms(settings), wait_timeout=0):
                final_state = await _invoke_graph_with_timeout(
                    sku_id, run_id, as_of, on_event=on_event, thread_id=tid,
                )
                status = str(final_state.get("status") or "ok")
                await asyncio.to_thread(_persist_result, run_id, {
                    "final_state": final_state,
                    "sku_id": sku_id,
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                })
    except LockTimeoutError as exc:
        error = str(exc)
    except Exception as exc:  # 采集/LLM 失败也要落库，便于排障
        error = f"{type(exc).__name__}: {exc}"
    finally:
        # 记录线程活跃时间并做上限淘汰（checkpointer 持久化后仍需要控制线程数）
        try:
            await touch_thread(tid)
            await prune_threads()
        except Exception:  # noqa: BLE001 —— 淘汰失败不影响本次研判结果
            pass

    return {
        "run_id": run_id,
        "sku_id": sku_id,
        "thread_id": tid,
        "status": status,
        "error": error,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "final_state": final_state,
    }


class ThreadNotFoundError(RuntimeError):
    """追问的 thread 不存在（还没跑过该 SKU 的研判）。"""


class ThreadAccessError(PermissionError):
    """追问线程与请求的 SKU 不一致：防止用别人的 thread_id 读到其他 SKU/部门的采集数据。

    这是「校验了参数、没校验数据来源」的经典漏洞：
    `/api/inspect/followup` 只校验了请求里的 sku_id 是否属于调用者部门，
    但真正读的 state 来自请求里的 thread_id —— 男装账号传一个女装线程的 id，
    就能拿到女装 SKU 的采集数据与结论。因此这里以 state 里的 sku_id 为准做二次校验。
    """


async def followup_sku(sku_id: str, instruction: str, thread_id: str | None = None) -> dict:
    """多轮追问：复用上一轮采集数据，只重跑 reasoner/verifier（不重复调 MCP）。

    实现：checkpointer 里存着上一轮 full state；用 `Command(goto="refine")` 从 refine
    节点重新进入图，reasoner 读取 state["followup"] 回答追问。
    """
    from langgraph.types import Command

    tid = thread_id or f"sku:{sku_id}"
    config = {"configurable": {"thread_id": tid}}
    started = time.monotonic()

    def _load_department() -> str | None:
        with session_scope() as session:
            sku = repos.get_sku(session, sku_id)
            return department_of(sku.category) if sku else None

    sku_department = await asyncio.to_thread(_load_department)   # 同 inspect_sku：同步 DB 出循环
    async with inspection_graph() as graph:
        snapshot = await graph.aget_state(config)
        values = getattr(snapshot, "values", None) if snapshot is not None else None
        if not values:
            # 不回显调用方传入的 thread_id：它是用户可控输入，原样反射回响应属于信息回显；
            # 这里只给"怎么解决"的提示。
            raise ThreadNotFoundError(
                "分析线程不存在：请先对该 SKU 发起一次研判，再追问"
            )
        # 越权防线：以 state 里的 sku_id 为准（不是以调用方传入的 sku_id 为准）。
        # 线程若属于别的 SKU，直接拒绝——否则跨部门数据会随追问接口泄露。
        state_sku = str(values.get("sku_id") or "")
        if state_sku and state_sku != sku_id:
            # 不回显线程里的 SKU：thread_id 是调用方输入，把「这条线程属于谁」反射回去
            # 等于给探测者额外信息；只说明「不一致」，让人去核对自己传的 thread_id。
            raise ThreadAccessError(
                "分析线程与请求的 SKU 不一致（该线程属于其它 SKU），已拒绝访问"
            )
        final: dict = dict(values)

        async def _run_refine() -> None:
            with department_context(sku_department):
                async for chunk in graph.astream(
                    Command(goto="refine", update={"mode": "followup", "followup": instruction}),
                    config=config, stream_mode="updates",
                ):
                    for node, update in (chunk or {}).items():
                        if isinstance(update, dict) and not node.startswith("__"):
                            _merge_updates(final, update)

        # 追问同样要有硬上限：它也要跑 reasoner/verifier，模型卡住时会一直占着请求
        timeout = float(getattr(get_settings(), "inspect_timeout_seconds", 0.0) or 0.0)
        try:
            # 复审 F2：追问也先 touch，避免本线程在等待期间被并发裁剪
            try:
                await touch_thread(tid)
            except Exception:  # noqa: BLE001
                pass
            if timeout > 0:
                await asyncio.wait_for(_run_refine(), timeout=timeout)
            else:
                await _run_refine()
        except asyncio.TimeoutError as exc:
            logger.warning("追问超时（>%.0fs）：sku=%s", timeout, sku_id)
            raise InspectTimeoutError(f"追问超时（>{timeout:.0f}s），已中止") from exc
    final["status"] = str(final.get("status") or "ok")
    try:
        await touch_thread(tid)
        await prune_threads()
    except Exception:  # noqa: BLE001
        pass
    return {
        "thread_id": tid,
        "sku_id": sku_id,
        "instruction": instruction,
        "status": final.get("status"),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "final_state": final,
    }


async def run_patrol(
    mode: str = "daily",
    limit: int | None = None,
    run_id: str | None = None,
    department: str | None = None,
    operator: str = "system",
) -> dict:
    """批量巡检：筛查 → Stream 入队（幂等）→ 消费组批量消费 → 逐 SKU 研判 → ACK。

    - 支持实时进度（写入 patrol_progress，供看板展示当前链路节点与参数）；
    - 支持协作式终止：request_stop(run_id) 后，当前批次结束后停止并输出已巡检/未巡检/中断原因。
    """
    started = time.monotonic()
    settings = get_settings()
    run_id = run_id or str(uuid.uuid4())
    stop_event = patrol_progress.get_stop_event(run_id)
    patrol_progress.start_run(run_id, params={
        "mode": mode, "limit": limit, "department": department, "operator": operator,
    })

    def _p(**fields):
        patrol_progress.update(run_id, **fields)

    skipped_recent = 0
    _p(stage="screening", message="正在执行多维度量化筛查…")
    # 量化筛选用同步 SQLAlchemy：放线程里跑，避免把 4C 机器上的事件循环卡住
    # （否则巡检进行中，对话的 SSE 心跳与取数会被拖慢）。
    # 筛查会 DELETE+INSERT 同一小时的 anomaly 行，必须互斥：抢不到锁就复用库里现成的结果。
    screen_hour = dt.datetime.now().strftime("%Y%m%d%H")
    screened_by_us = True
    try:
        async with redis_lock(f"lock:screen:{screen_hour}", ttl_ms=300_000, wait_timeout=0):
            anomalies, sku_cat, screened_total, skipped_recent = await asyncio.to_thread(
                _screen_and_filter, mode, department,
            )
    except LockTimeoutError:
        screened_by_us = False
        _p(stage="screening",
           message="另一个实例正在执行筛查，本轮直接复用已有异常列表（避免重复写 anomaly）")
        anomalies, sku_cat, screened_total, skipped_recent = await asyncio.to_thread(
            _screen_and_filter, mode, department, run_screen=False,
        )
    candidates = len(anomalies)
    anomalies = anomalies[: limit or MAX_PATROL_SKUS]
    selected = len(anomalies)
    _p(stage="enqueue", message=f"筛查完成：命中 {screened_total}，候选 {candidates}，本次处理 {selected}，正在入队…",
       total_selected=selected, processed=0)

    queue = get_stream_queue()
    dedupe_hour = dt.datetime.now().strftime("%Y%m%d%H")
    enqueued, deduped = 0, 0
    for a in anomalies:
        dept = department_of(sku_cat.get(a.sku_id))
        _, dup = await queue.aenqueue(
            {"sku_id": a.sku_id, "mode": mode, "hour": dedupe_hour, "department": dept},
            dedupe_key=f"{a.sku_id}:{dedupe_hour}",
        )
        if dup:
            deduped += 1
        else:
            enqueued += 1
    _p(stage="consume", message=f"入队完成：入队 {enqueued}（幂等拦截 {deduped}），开始消费并逐 SKU 研判…",
       total_selected=selected, processed=0, queue=await queue.astats())

    results: list[dict] = []
    skipped_by_stop = 0
    semaphore = asyncio.Semaphore(max(1, settings.patrol_concurrency))
    interrupted = False
    # 队列快照：进度事件里的 queue 字段用上一次 ack 后取到的值，
    # 不在节点回调里同步读 Redis（回调跑在事件循环里，同步 Redis 会卡住整个进程）
    queue_snapshot = await queue.astats()
    # 先把「上次投递了但没 ACK」的遗留消息捞回来（进程崩溃 / 异常退出 / 客户端中断）：
    # 没有这一步，这些消息会永远留在 PEL 里，SKU 悄悄少跑且无人知晓。
    reclaimed_batch, dead_lettered = await queue.aclaim_stale()
    if reclaimed_batch or dead_lettered:
        _p(stage="reclaim",
           message=(f"回收未确认消息 {len(reclaimed_batch)} 条"
                    + (f"，{dead_lettered} 条超过重投上限进死信" if dead_lettered else "")),
           processed=len(results), total_selected=selected, queue=queue_snapshot)
    pending_batches: list[list[tuple[str, dict]]] = (
        [reclaimed_batch] if reclaimed_batch else []
    )
    reclaimed_count = len(reclaimed_batch)

    async def _process_one(msg_id: str, task: dict) -> None:
        nonlocal skipped_by_stop, queue_snapshot
        sku = task.get("sku_id", "")
        if stop_event.is_set():
            skipped_by_stop += 1
            await queue.aack(msg_id)
            return

        def _on_node(node: str, ms: int, _update: dict) -> None:
            _p(stage="inspect", sku_id=sku, node=node, node_ms=ms,
               message=f"正在研判 {sku} → 节点「{node}」({ms}ms)",
               processed=len(results), total_selected=selected,
               queue=queue_snapshot)

        async with semaphore:
            if stop_event.is_set():
                skipped_by_stop += 1
                await queue.aack(msg_id)
                return
            _p(stage="inspect_start", sku_id=sku, node=None,
               message=f"开始研判 {sku}", processed=len(results),
               total_selected=selected, queue=queue_snapshot)
            res = await inspect_sku(sku, run_id, as_of=dt.date.today().isoformat(), on_event=_on_node)
        results.append(res)
        await queue.aack(msg_id)
        queue_snapshot = await queue.astats()
        _p(stage="inspect", sku_id=sku, node="done",
           message=f"{sku} 研判完成（{res['status']}）",
           processed=len(results), total_selected=selected, queue=queue_snapshot)

    while True:
        if stop_event.is_set():
            interrupted = True
            break
        # 先处理回收来的遗留消息，再取新消息
        if pending_batches:
            batch = pending_batches.pop(0)
        else:
            batch = await queue.aconsume(count=10)
        if not batch:
            break
        await asyncio.gather(*[_process_one(mid, task) for mid, task in batch])

    if stop_event.is_set():
        interrupted = True
    ok = sum(1 for r in results if r["status"] == "ok")
    degraded = sum(1 for r in results if r["status"] == "degraded")
    failed = sum(1 for r in results if r["status"] == "failed")
    inspected = len(results)
    not_inspected = max(0, selected - inspected)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    interrupt_reason = "用户中断" if interrupted else None
    run_status = "cancelled" if interrupted else "ok"

    # 本次巡检简报：风险分布 + 优先关注动作
    risk_dist = {"高": 0, "中": 0, "低": 0}
    top_actions: list[dict] = []
    for r in results:
        final = r.get("final_state") or {}
        plan = final.get("plan") or {}
        risk = str(plan.get("risk_level") or "中")
        risk_dist[risk] = risk_dist.get(risk, 0) + 1
        actions = plan.get("plan") or []
        if actions and len(top_actions) < 5:
            top_actions.append({
                "sku_id": r.get("sku_id"),
                "risk_level": risk,
                "action": actions[0].get("action"),
                "qty": actions[0].get("qty"),
            })
    dept_text = f"{department}部门" if department else "全部门"
    mode_text = "每日全量" if mode == "daily" else "小时增量"
    summary_text = (
        f"{operator} 执行{mode_text}巡检（{dept_text}）："
        f"全量筛查 {screened_total} 个异常，跳过最近 1 小时已处理 {skipped_recent} 个，"
        f"候选 {candidates} 个，本次处理 {selected} 个（已巡检 {inspected}、成功 {ok}、降级 {degraded}、失败 {failed}），"
        f"耗时 {elapsed_ms / 1000:.1f}s；风险分布 高{risk_dist.get('高', 0)}/中{risk_dist.get('中', 0)}/低{risk_dist.get('低', 0)}。"
    )
    if interrupted:
        summary_text += f" ⚠ 巡检被终止（{interrupt_reason}）：未巡检 {not_inspected} 个。"
    report = {
        "summary": summary_text,
        "mode": mode,
        "department": department,
        "operator": operator,
        "counts": {
            "screened": screened_total, "skipped_recent": skipped_recent,
            "candidates": candidates, "selected": selected, "processed": inspected,
            "ok": ok, "degraded": degraded, "failed": failed,
            "skipped_by_stop": skipped_by_stop,
            "reclaimed": reclaimed_count, "dead_lettered": dead_lettered,
        },
        "risk_distribution": risk_dist,
        "top_actions": top_actions,
        "elapsed_ms": elapsed_ms,
        "interrupted": interrupted,
        "interrupt_reason": interrupt_reason,
        "inspected_count": inspected,
        "not_inspected_count": not_inspected,
        "next_steps": (
            "异常 SKU 已生成处置预案并通过校验 Agent 合规检查并落库；"
            "高风险补货动作需在「待确认」中确认后才会流转 Mock ERP，其余动作可在异常列表与研判结果中查看。"
        ),
    }
    summary = {
        "run_id": run_id,
        "run_status": run_status,
        "mode": mode,
        "department": department,
        "operator": operator,
        "screened": screened_total,
        "skipped_recent": skipped_recent,
        "candidates": candidates,
        "selected": selected,
        "enqueued": enqueued,
        "deduped": deduped,
        "processed": inspected,
        "ok": ok,
        "degraded": degraded,
        "failed": failed,
        "elapsed_ms": elapsed_ms,
        "risk_distribution": risk_dist,
        "interrupted": interrupted,
        "interrupt_reason": interrupt_reason,
        "inspected_count": inspected,
        "not_inspected_count": not_inspected,
        # 重投/死信可观测：回收到了多少遗留消息、多少条被判"毒消息"
        "reclaimed": reclaimed_count,
        "dead_lettered": dead_lettered,
        # 本轮是否由自己执行筛查（false=另一个实例正在筛，复用了现成结果）
        "screened_by_us": screened_by_us,
        "report": report,
        "queue": await queue.astats(),
    }

    # 持久化巡检日志与简报（保留全部历史，页面展示最近 N 条）
    with session_scope() as session:
        session.add(PatrolLog(
            run_id=run_id, operator=operator, mode=mode, department=department,
            screened=screened_total, skipped_recent=skipped_recent, candidates=candidates,
            selected=selected, processed=inspected, ok=ok, degraded=degraded, failed=failed,
            elapsed_ms=elapsed_ms, report=report, created_at=dt.datetime.now(),
        ))

    _p(stage="cancelled" if interrupted else "finished",
       message=(f"巡检已终止：已巡检 {inspected}，未巡检 {not_inspected}（{interrupt_reason}）"
                if interrupted else f"巡检完成：处理 {inspected} 个 SKU"),
       processed=inspected, total_selected=selected, interrupted=interrupted,
       queue=queue_snapshot)
    return summary
