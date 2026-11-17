# 一页纸：这个项目是什么

> 给快速浏览者。3 分钟读完，不需要读代码。
> 想看细节：架构 → [ARCHITECTURE.md](ARCHITECTURE.md)｜接口 → [API.md](API.md)｜部署 → [DEPLOY.md](DEPLOY.md)

---

## 一句话

**服装电商「款 × 色 × 码」SKU 库存监管 Agent**：用「量化初筛 + LangGraph 多 Agent 研判 + 校验纠偏 + 引用溯源」替代人肉看报表；对话侧是带**出口溯源闸门**的 ReAct Agent，全链路本地可跑、可评测、可降级。

## 技术栈（一行）

Python 3.11 · LangGraph 1.2（StateGraph/subgraph/checkpointer）· LangChain Tools · FastMCP（memory/stdio/direct）· DeepSeek · FastAPI + SSE · SQLite/MySQL · Redis/fakeredis · Qdrant · 原生 JS 看板（无构建）· Docker Compose

## 五个工程要点（都有可跑的验证）

| # | 要点 | 证据 |
|---|---|---|
| 1 | **研判分阶段编排**：Supervisor 定采集子集 → 并发采集 → 两个 Skill 子图 → Reasoner → **Verifier 规则硬校验 + 失败回流（≤2 次）**，超限显式 `degraded` | `agents/graph.py`、`tests/test_graph_routing.py`（未派发的 source 0 次 MCP 调用） |
| 2 | **数字可溯源**：带单位数字必须能追溯到本轮 `result_id`；不达标先重写一次，仍不达标标 `degraded` 并挂告警条 | `agents/grounding.py`、`tests/test_grounding.py`、`tests/test_red_team.py` |
| 3 | **工具层工程化**：3 个 MCP server / 15 工具；同 server 2 会话连接池 + 3 次失败熔断；每工具成功率 / P95 / 失败分类 | `mcp_client/client.py`、`scripts/bench_mcp.py`（pool=2 ≈2.0x）、`observability/tool_stats.py` |
| 4 | **多轮状态可恢复**：研判图挂 SQLite checkpointer，追问复用上一轮采集（MCP 增量 0）；**重启服务后仍能继续追问** | `agents/checkpoint.py`、`tests/test_checkpoint_persistence.py`、`/api/health.checkpoint` |
| 5 | **安全边界在机制层**：出口溯源闸门、部门作用域、写工具白名单（读写分离）、四类注入/越权回归用例 | `agents/grounding.py`、`platform_tools.py`、`tests/test_red_team.py`、`tests/test_followup_authz.py` |
| 6 | **Agent 可热插拔**：注册表驱动 3 个 Agent（分析 / 诊断 / 动作）+ 确定性路由（26 条 golden set 全对，含 intent 分支与工具能力断言），新增 Agent 丢一个 json + md 即可，无需重启 | `agents/registry.py`、`registry/*.json`、`scripts/eval_routing.py` |

## 三个数字

- **1061** 条离线测试通过，覆盖率 **89%**，`ruff` 全绿（CI 十二步：lint → compileall → pytest+覆盖率门槛 → 答案质量自检 → 检索门槛 → 路由门槛 → 作用域矩阵 → 溯源闸门 → **中台治理矩阵** → **Agent 工具面/写路径** → 并发写自检 → 前端冒烟）+ MySQL service 跑关键子集
- 检索 **R@1 100% / R@3 100% / R@5 100% / MRR 1.000**（38 题；语料 14 篇 / 67 chunk，定位为回归门槛；消融 bm25 92.1% / vector 94.7% / rrf 100%）
- 对话并发：闸门 2 → 5.2s / 4.7 rps，闸门 8 → 2.7s / 9.0 rps（`scripts/bench_chat.py --mock`，模型桩 300ms、24 请求）
- 上下文缓存：提示词「静态在前、动态在后」，命中率见 `run_detail.llm.prompt_cache`（本地连问 3 轮实测 31%~71%，随 Agent 切换与工具结果长度波动）

## 30 秒跑起来

```bash
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env                       # 填 DEEPSEEK_API_KEY（不填也能跑测试与看板）
PYTHONPATH=src python scripts/init_db.py && PYTHONPATH=src python scripts/platform_bootstrap.py
PYTHONPATH=src uvicorn app.main:app --port 8000                 # 看板 http://127.0.0.1:8000/（Agent 目录：/agents.html）
PYTHONPATH=src uvicorn data_platform.api.main:app --port 8001   # 数据中台
```

账号：`admin/hhw123`（管理员）、`worker1/hhw111`（男装）、`worker2/hhw222`（女装）。

## 能力边界

- **数据层**：SKU/库存/销量/订单/物流均为程序生成的演示数据，外部系统以 REST / MCP 契约固定；替换为真实系统时改接入地址或替换 MCP server 实现即可，工具签名不变。
- **运行形态**：默认零外部依赖（SQLite + fakeredis + 内存向量），需要生产级组件时按 `docs/CONFIG.md` 开启 MySQL / Redis / Qdrant / OTLP。
- **评测口径**：检索题集 38 条用于回归对比；LLM 输出有采样波动，评测脚本打印每次实际运行值。

## 从这里往下走

| 想做什么 | 看哪个 |
|---|---|
| 快速判断技术深度 | [ARCHITECTURE.md](ARCHITECTURE.md)（分层数据流 / 图编排 / 检索栈 / 降级阶梯） |
| 对接接口或写脚本 | [API.md](API.md)（Agent / 中台 / MCP 工具 + 关键响应字段） |
| 真跑一遍 | 仓库根 `README.md`「快速启动」/ [DEPLOY.md](DEPLOY.md) |
| 调参数 | [CONFIG.md](CONFIG.md)（全部环境变量与默认值） |
