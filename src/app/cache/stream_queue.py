"""巡检任务队列：Redis Stream + 消费组 + 幂等去重（Redis Stream 幂等 + 批量消费）。"""

from __future__ import annotations

import asyncio
import json
import logging

from app.cache.redis_factory import get_redis
from app.config import Settings, get_settings

logger = logging.getLogger("sku-agent.queue")

# 投递次数 / 死信的键（生产可观测：pending 涨、dead 有值时要有人处理）
_DELIVERIES_KEY = "queue:deliveries"
_DEAD_KEY = "queue:dead"


class StreamQueue:
    def __init__(self, client=None, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self.client = client or get_redis()
        self.key = self._settings.stream_key
        self.group = self._settings.stream_group
        self._ensure_group()

    def _ensure_group(self) -> None:
        try:
            self.client.xgroup_create(self.key, self.group, id="0", mkstream=True)
        except Exception as exc:  # BUSYGROUP：组已存在
            if "BUSYGROUP" not in str(exc):
                raise

    # ------------------------------------------------------------ 生产
    def enqueue(self, task: dict, dedupe_key: str | None = None, dedupe_ttl: int = 86_400) -> tuple[str | None, bool]:
        """入队；dedupe_key 相同且窗口内已存在时返回 (None, True) 表示被幂等拦截。"""
        if dedupe_key:
            dedupe_name = f"dedupe:{self.key}:{dedupe_key}"
            acquired = self.client.set(dedupe_name, "1", nx=True, ex=dedupe_ttl)
            if not acquired:
                return None, True
        msg_id = self.client.xadd(
            self.key, {"payload": json.dumps(task, ensure_ascii=False)}
        )
        # 控制 stream 长度，避免长期运行无限增长（近似裁剪，性能友好）
        try:
            self.client.xtrim(self.key, maxlen=1000, approximate=True)
        except Exception:
            pass
        return str(msg_id), False

    def pending_count(self) -> int:
        try:
            info = self.client.xinfo_groups(self.key)
            for g in info:
                if g.get("name") == self.group:
                    return int(g.get("pending", 0))
        except Exception:
            pass
        return 0

    # ------------------------------------------------------------ 消费
    def _mark_delivery(self, msg_id: str) -> int:
        """记一次投递并返回累计次数（用于判定是否进死信）。"""
        try:
            return int(self.client.hincrby(_DELIVERIES_KEY, msg_id, 1))
        except Exception:  # noqa: BLE001 —— 计数失败不影响消费
            return 0

    def _dead_letter(self, msg_id: str, task: dict, reason: str) -> None:
        """投递次数超限的「毒消息」进死信：不再无限重投，留下证据供排查。"""
        try:
            self.client.hset(_DEAD_KEY, msg_id, json.dumps(
                {"task": task, "reason": reason}, ensure_ascii=False,
            ))
        except Exception as exc:  # noqa: BLE001
            logger.warning("写死信失败 msg=%s：%s", msg_id, exc)

    def _decode(self, msg_id: str, fields: dict | None) -> tuple[str, dict] | None:
        if not fields:
            return None
        try:
            task = json.loads(fields.get("payload", "{}"))
        except json.JSONDecodeError:
            task = {}
        msg_id = str(msg_id)
        dept = task.get("department")
        if dept:
            # 记录消息归属部门：用于按部门统计 pending / length
            try:
                self.client.hset("queue:msg_dept", msg_id, dept)
            except Exception:  # noqa: BLE001
                pass
        return msg_id, task

    def consume(self, consumer: str | None = None, count: int = 10, block_ms: int = 0) -> list[tuple[str, dict]]:
        consumer = consumer or self._settings.stream_consumer
        resp = self.client.xreadgroup(
            self.group, consumer, {self.key: ">"}, count=count, block=block_ms or None
        )
        out: list[tuple[str, dict]] = []
        if not resp:
            return out
        for _stream, entries in resp:
            for msg_id, fields in entries:
                decoded = self._decode(msg_id, fields)
                if decoded is None:
                    continue
                self._mark_delivery(decoded[0])
                out.append(decoded)
        return out

    def claim_stale(self, consumer: str | None = None, min_idle_ms: int | None = None,
                    count: int | None = None) -> tuple[list[tuple[str, dict]], int]:
        """回收「已投递但没 ACK」的消息（XAUTOCLAIM），返回 (可处理消息, 进死信条数)。

        为什么必须有：`consume` 用 ">" 只能拿到新消息，进了 PEL 却没 ACK 的消息
        （进程崩溃 / 研判异常退出 / 客户端中断）永远不会再被投递 —— 表现就是
        「pending 一直涨、SKU 悄悄少跑了几个」，而且没有任何报错。
        """
        consumer = consumer or self._settings.stream_consumer
        min_idle = int(min_idle_ms if min_idle_ms is not None
                       else getattr(self._settings, "queue_min_idle_ms", 60_000))
        batch = int(count or getattr(self._settings, "queue_claim_batch", 20))
        max_deliveries = int(getattr(self._settings, "queue_max_deliveries", 3))
        try:
            resp = self.client.xautoclaim(
                self.key, self.group, consumer,
                min_idle_time=min_idle, start_id="0-0", count=batch,
            )
        except Exception as exc:  # noqa: BLE001 —— 回收失败不能把消费循环打断
            logger.warning("XAUTOCLAIM 失败（本轮跳过回收）：%s", exc)
            return [], 0
        entries = []
        if isinstance(resp, (list, tuple)) and len(resp) >= 2:
            entries = resp[1] or []
        out: list[tuple[str, dict]] = []
        dead = 0
        for item in entries:
            if not item:
                continue
            msg_id, fields = (item + (None, None))[:2] if isinstance(item, (list, tuple)) else (None, None)
            decoded = self._decode(msg_id, fields) if msg_id else None
            if decoded is None:
                continue
            deliveries = self._mark_delivery(decoded[0])
            if deliveries > max_deliveries:
                self._dead_letter(decoded[0], decoded[1],
                                  f"投递次数 {deliveries} 超过上限 {max_deliveries}")
                self.ack(decoded[0])
                dead += 1
                continue
            out.append(decoded)
        return out, dead

    def ack(self, *msg_ids: str) -> int:
        if not msg_ids:
            return 0
        dept_map = self.client.hgetall("queue:msg_dept") or {}
        for msg_id in msg_ids:
            if msg_id in dept_map:
                self.client.hdel("queue:msg_dept", msg_id)
            self.client.hdel(_DELIVERIES_KEY, msg_id)
        return int(self.client.xack(self.key, self.group, *msg_ids))

    def stats(self, department_resolver=None) -> dict:
        """返回队列统计；departments 提供分部门的 length/pending（按消息 payload.department）。"""
        try:
            length = int(self.client.xlen(self.key))
        except Exception:
            length = 0

        departments: dict[str, dict] = {}
        try:
            entries = self.client.xrange(self.key, "-", "+", count=1000) or []
        except Exception:
            entries = []
        for _msg_id, fields in entries:
            try:
                task = json.loads(fields.get("payload", "{}"))
            except json.JSONDecodeError:
                task = {}
            dept = task.get("department")
            if not dept and department_resolver and task.get("sku_id"):
                try:
                    dept = department_resolver(task["sku_id"])
                except Exception:
                    dept = None
            dept = dept or "未知"
            bucket = departments.setdefault(dept, {"length": 0, "pending": 0})
            bucket["length"] += 1

        # PEL 中未被 ACK 的消息按部门计数（consume 时记录了 msg_id → dept）
        try:
            pending_map = self.client.hgetall("queue:msg_dept") or {}
            for _msg_id, dept in pending_map.items():
                dept = dept or "未知"
                bucket = departments.setdefault(dept, {"length": 0, "pending": 0})
                bucket["pending"] += 1
        except Exception:
            pass

        return {
            "stream": self.key,
            "group": self.group,
            "length": length,
            "pending": self.pending_count(),
            "departments": departments,
            # 可观测：deliveries 是正在跟踪投递次数的消息数，dead 是进死信的消息数。
            # 生产上 dead 一旦非 0 就该有人去看，而不是让它静静躺着。
            "tracking_deliveries": self._hash_len(_DELIVERIES_KEY),
            "dead": self._hash_len(_DEAD_KEY),
        }

    def _hash_len(self, key: str) -> int:
        try:
            return int(self.client.hlen(key) or 0)
        except Exception:  # noqa: BLE001
            return 0

    def dead_letters(self) -> dict[str, dict]:
        """死信明细（排障用）：msg_id → {task, reason}。"""
        try:
            rows = self.client.hgetall(_DEAD_KEY) or {}
        except Exception:  # noqa: BLE001
            return {}
        out: dict[str, dict] = {}
        for msg_id, raw in rows.items():
            try:
                out[str(msg_id)] = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                out[str(msg_id)] = {"raw": raw}
        return out

    # ------------------------------------------------------------ 异步包装（不阻塞事件循环）
    # 与 TTLCache 同理：redis-py 同步客户端在事件循环里读，一次网络抖动就会冻住
    # SSE 心跳和其它会话。巡检是长任务，更容易撞上，所以队列读写一律走 to_thread。
    async def aenqueue(self, task: dict, dedupe_key: str | None = None,
                       dedupe_ttl: int = 86_400) -> tuple[str | None, bool]:
        return await asyncio.to_thread(self.enqueue, task, dedupe_key, dedupe_ttl)

    async def aconsume(self, consumer: str | None = None, count: int = 10,
                       block_ms: int = 0) -> list[tuple[str, dict]]:
        return await asyncio.to_thread(self.consume, consumer, count, block_ms)

    async def aclaim_stale(self, consumer: str | None = None, min_idle_ms: int | None = None,
                           count: int | None = None) -> tuple[list[tuple[str, dict]], int]:
        return await asyncio.to_thread(self.claim_stale, consumer, min_idle_ms, count)

    async def aack(self, *msg_ids: str) -> int:
        return await asyncio.to_thread(self.ack, *msg_ids)

    async def astats(self, department_resolver=None) -> dict:  # noqa: ANN001
        return await asyncio.to_thread(self.stats, department_resolver)


_queue: StreamQueue | None = None


def get_stream_queue() -> StreamQueue:
    global _queue
    if _queue is None:
        _queue = StreamQueue()
    return _queue


def reset_stream_queue() -> None:
    global _queue
    _queue = None
