"""Agent 注册表：manifest 驱动的 Agent 装配 + 热重载。

为什么需要这一层：
- 之前 Agent 是"代码内硬装配"（`build_chat_agent` 里写死提示词文件名、模型参数、全量工具），
  加一个 Agent 要改 4 个文件并重启；
- 现在把 Agent 变成**声明式数据**：`registry/*.json` + `prompts/<name>.md`。
  丢一个 json 就多一个 Agent，改 md 就更新提示词，`agent_id` 直接可调用——不需要改代码、不需要重启。

校验与容错原则：
- 单个 manifest 非法（缺 prompt、工具名拼错、id 重复）只登记到 `errors`，**不影响其它 Agent 启动**，
  避免一个坏文件把服务打挂；错误在 `/api/agents` 与 `/api/health.agents` 可见。
- `tools: ["*"]`（或空）表示全量工具，保持"只有一个 Agent 时行为与以前完全一致"。
"""

from __future__ import annotations

import hashlib
import json
import logging
import pathlib
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.agents.prompt_loader import PROMPT_DIR, prompt_version
from app.config import get_settings
from app.tools.platform_tools import WRITE_TOOL_NAMES, resolve_tool_selection

logger = logging.getLogger("sku-agent.registry")

# `registry.load()` 校验 `routing.intents` 用的是 `query_plan.INTENTS`（唯一的意图来源）。
# 延迟导入：避免 registry ← query_plan ← memory ← … 在冷启动时互相牵扯的顺序问题。
_INTENTS_CACHE: tuple[str, ...] | None = None


def _query_plan_intents() -> tuple[str, ...]:
    global _INTENTS_CACHE
    if _INTENTS_CACHE is None:
        from app.agents.query_plan import INTENTS

        _INTENTS_CACHE = tuple(INTENTS)
    return _INTENTS_CACHE

_AGENT_ID_ALLOWED = set("abcdefghijklmnopqrstuvwxyz0123456789_-")

# 路由线索词：解释词（要的是说明）与动作/症状词（要的是诊断或执行）
# 提示词模板允许出现的占位符：写错（如 {metdata}）会在加载时报错，避免静默降质
ALLOWED_PROMPT_PLACEHOLDERS = {
    "today", "username", "role_text", "memory_sections", "metadata",
    "replenish_qty_threshold", "replenish_amount_threshold", "memory_max_inline_rows",
}
REQUIRED_PROMPT_PLACEHOLDERS = {"metadata", "memory_sections"}
MEMORY_SCOPES = {"session", "none"}

EXPLAIN_HINTS = ("解释", "口径", "区别", "是什么", "是什么意思", "怎么算", "定义", "原理")
ACTION_HINTS = ("建单", "创建", "补货单", "下单", "退货", "退款", "售后", "审批", "物流",
                 "快递", "发货", "订单", "断码", "断货", "缺货", "售罄", "积压", "补货量",
                 "补多少", "调拨", "动销")


class AgentRouting(BaseModel):
    """路由线索：关键词（确定性优先）与 query_plan.intent 映射。"""

    model_config = ConfigDict(extra="forbid")

    keywords: list[str] = Field(default_factory=list)
    intents: list[str] = Field(default_factory=list)


class AgentManifest(BaseModel):
    """一个 Agent 的完整声明（数据即配置）。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str = ""
    description: str = ""
    prompt: str
    # 缺省只读：忘写 tools 不该意外拿到写工具（要全量必须显式写 ["*"]）
    tools: list[str] = Field(default_factory=lambda: ["read"])
    model: str = ""                 # 空 = 继承全局 LLM_MODEL
    temperature: float = 0.2
    max_tokens: int = 2500
    memory_scope: str = "session"   # session / none（预留：org/private）
    max_reflection_rounds: int = 0  # 预留：生图/反思类 Agent 的循环上限
    # 单轮允许的**成功**工具调用次数（P1-1 工具循环守护）：超了立刻终止本轮并提示重试。
    # 依据：演示库 137 轮真实对话里单轮工具调用中位 2~3、最大 11，默认 15 留余量；
    # 窄工具的 Agent（诊断/执行）收紧到 12/6，避免"一个问题把预算花在无关工具上"。
    max_tool_calls: int = 15
    routing: AgentRouting = Field(default_factory=AgentRouting)
    enabled: bool = True
    default: bool = False           # 默认 Agent（不设则取 id 字典序第一个）
    # ---- 运行时计算字段（不参与校验，回给 API 用）----
    version: str = ""
    prompt_version: str = ""
    tool_names: list[str] = Field(default_factory=list)
    # 只读 Agent 被剔除的写工具（P2-4：声明不含 write/`*` 时自动剔除，避免"只读"名不副实）
    stripped_write_tools: list[str] = Field(default_factory=list)

    @field_validator("max_tool_calls")
    @classmethod
    def _check_max_tool_calls(cls, value: int) -> int:
        if not 1 <= int(value) <= 40:
            raise ValueError("max_tool_calls 需在 1~40（单轮成功工具调用预算）")
        return int(value)

    @field_validator("max_reflection_rounds")
    @classmethod
    def _check_reflection_rounds(cls, value: int) -> int:
        if not 0 <= int(value) <= 5:
            raise ValueError("max_reflection_rounds 需在 0~5（预留字段，生图/反思类 Agent 用）")
        return int(value)

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not value or len(value) > 32 or set(value) - _AGENT_ID_ALLOWED or not value[0].isalpha():
            raise ValueError("id 需为 2~32 位小写字母开头，仅含 a-z0-9_-")
        return value


_PLACEHOLDER_RE = __import__("re").compile(r"\{([a-z_][a-z0-9_]*)\}")


def _groups_of(manifest: "AgentManifest", selected: set[str] | None) -> set[str]:
    """该 manifest 显式声明了哪些工具分组（用于「写工具是否被意外放行」这类告警）。"""
    return {item for item in manifest.tools if item.islower()}


class AgentRegistry:
    """目录扫描 + 校验 + 版本计算 + 热重载。"""

    def __init__(self, registry_dir: pathlib.Path | str | None = None) -> None:
        self._explicit_dir = pathlib.Path(registry_dir) if registry_dir else None
        self._agents: dict[str, AgentManifest] = {}
        self._errors: list[dict[str, str]] = []
        self._loaded_at: float = 0.0
        self._mtime: tuple[float, float, float, str] = (0.0, 0.0, 0.0, "")

    # ------------------------------------------------------------ 路径
    def registry_dir(self) -> pathlib.Path:
        if self._explicit_dir is not None:
            return self._explicit_dir
        configured = (getattr(get_settings(), "agents_registry_dir", "") or "").strip()
        if configured:
            return pathlib.Path(configured)
        return pathlib.Path(__file__).resolve().parent / "registry"

    def _dir_mtime(self) -> tuple[float, float, float, str]:
        """热加载指纹 = (registry 目录 mtime, registry 内文件最大 mtime, prompts 内文件最大 mtime, 清单摘要)。

        为什么不能只看目录 mtime：**修改一个已存在的文件不会改变父目录的 mtime**（Windows/Linux 都一样），
        只盯着目录会导致"改 manifest 不生效、只有新增/删除才生效"——这是线上验收时抓到的真实缺陷。

        为什么还要带「清单摘要」（文件名 + 大小 + mtime_ns 的 sha1）：文件系统的 mtime 粒度不一定比
        两次写入更细。容器 overlayfs 上实测出现过"新建 manifest 与上一次写落在同一时间片、mtime
        完全相同"，只比 mtime 会漏掉新增/改名。把文件名与大小并进指纹后，这类变更也能确定性检出；
        文件数很少（<20），每次 stat 一遍 + 一个短 sha1 的成本可以忽略。
        """

        def _stat(path: pathlib.Path) -> float:
            try:
                return path.stat().st_mtime
            except OSError:
                return 0.0

        def _max_file_mtime(directory: pathlib.Path, pattern: str) -> float:
            try:
                return max((_stat(p) for p in directory.glob(pattern)), default=0.0)
            except OSError:
                return 0.0

        def _listing(directory: pathlib.Path, pattern: str) -> str:
            items: list[str] = []
            try:
                for path in sorted(directory.glob(pattern)):
                    try:
                        st = path.stat()
                    except OSError:
                        continue
                    items.append(f"{path.name}:{st.st_size}:{st.st_mtime_ns}")
            except OSError:
                return ""
            return hashlib.sha1("|".join(items).encode("utf-8")).hexdigest()[:12]

        registry = self.registry_dir()
        return (
            _stat(registry),
            _max_file_mtime(registry, "*.json"),
            _max_file_mtime(PROMPT_DIR, "*.md"),
            _listing(registry, "*.json") + ":" + _listing(PROMPT_DIR, "*.md"),
        )

    # ------------------------------------------------------------ 加载
    def load(self) -> None:
        agents: dict[str, AgentManifest] = {}
        errors: list[dict[str, str]] = []
        for path in sorted(self.registry_dir().glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                manifest = AgentManifest(**raw)
                prompt_file = PROMPT_DIR / f"{manifest.prompt}.md"
                if not prompt_file.exists():
                    raise ValueError(f"提示词文件不存在：prompts/{manifest.prompt}.md")
                if manifest.memory_scope not in MEMORY_SCOPES:
                    raise ValueError(
                        f"memory_scope 只能是 {sorted(MEMORY_SCOPES)}，当前为 {manifest.memory_scope!r}"
                    )
                # 复审 P5：`routing.intents` 必须落在 query_plan 会产出的意图集合里。
                # 早先两个内置 manifest 写的是 "action"/"diagnosis"/"replenishment"/"order"，
                # 与 `query_plan.INTENTS`（lookup/diagnose/restock/…）完全不交集 →
                # 意图分支实际是死代码，路由评测也测不出来（它只传 message）。
                # 放进"加载期报错"这条既有约定里：拼错的意图不该等到线上才发现。
                bad_intents = [i for i in manifest.routing.intents
                               if i not in _query_plan_intents()]
                if bad_intents:
                    raise ValueError(
                        f"routing.intents 里有非法意图 {bad_intents}；"
                        f"可用 {list(_query_plan_intents())}（query_plan 只会产出这些）"
                    )
                prompt_text = prompt_file.read_text(encoding="utf-8")
                placeholders = set(_PLACEHOLDER_RE.findall(prompt_text))
                unknown = placeholders - ALLOWED_PROMPT_PLACEHOLDERS
                if unknown:
                    raise ValueError(
                        f"提示词里有未知占位符 {sorted(unknown)}；允许集合 {sorted(ALLOWED_PROMPT_PLACEHOLDERS)}"
                    )
                missing = REQUIRED_PROMPT_PLACEHOLDERS - placeholders
                warnings_local: list[str] = []
                if missing:
                    warnings_local.append(
                        f"提示词缺少占位符 {sorted(missing)}（记忆/口径可能不会注入）"
                    )
                selected = resolve_tool_selection(manifest.tools)
                # selected 只可能是 None（全量）或非空集合：`resolve_tool_selection`
                # 对「显式声明却解析为空」直接抛错，所以这里不会出现 tool_names=[]。
                # P2-4：声明里没有 write 组（也不是 "*"）的 Agent 视为只读，剔除写工具——
                # 否则 memory 组会把 save_analysis 带进来，admin 账号用它就能"只读 Agent 写档案"。
                if selected is not None:
                    declared = {item for item in manifest.tools if item}
                    if "write" not in declared:
                        stripped = selected & WRITE_TOOL_NAMES
                        if stripped:
                            selected = selected - stripped
                            manifest.stripped_write_tools = sorted(stripped)
                            warnings_local.append(
                                f"只读 Agent 已剔除写工具 {sorted(stripped)}（未声明 write 组）"
                            )
                manifest.tool_names = sorted(selected) if selected is not None else ["*"]
                if manifest.max_reflection_rounds and "image" not in _groups_of(manifest, selected):
                    warnings_local.append(
                        "max_reflection_rounds > 0 但工具集不含 image 组：反思循环尚未实现，该字段仅登记"
                    )
                manifest.prompt_version = prompt_version(manifest.prompt)
                digest = hashlib.sha1(
                    json.dumps(manifest.model_dump(exclude={"version", "prompt_version",
                                                            "tool_names", "stripped_write_tools"}),
                               ensure_ascii=False, sort_keys=True).encode("utf-8")
                    + manifest.prompt_version.encode("utf-8")
                ).hexdigest()[:8]
                manifest.version = digest
                if manifest.id in agents:
                    raise ValueError(f"agent id 重复：{manifest.id}")
                for message in warnings_local:
                    errors.append({"file": path.name, "level": "warning", "error": message})
                agents[manifest.id] = manifest
            except Exception as exc:  # noqa: BLE001 —— 单个文件坏掉不影响其它 Agent
                errors.append({"file": path.name, "level": "error",
                               "error": f"{type(exc).__name__}: {exc}"})
                logger.warning("加载 agent manifest 失败 %s：%s", path.name, exc)
        self._agents = agents
        self._errors = errors
        self._loaded_at = __import__("time").time()
        self._mtime = self._dir_mtime()

    def maybe_reload(self) -> bool:
        """目录 / 提示词 / 文件清单有变化就重新加载（每轮对话前调用，开销是十几次 stat + 两个短 sha1）。"""
        if not getattr(get_settings(), "agents_hot_reload", True):
            if not self._agents:
                self.load()
            return False
        if self._dir_mtime() != self._mtime or not self._agents:
            self.load()
            logger.info("agent 注册表已热重载：%s", sorted(self._agents))
            return True
        return False

    def reload(self) -> dict:
        self.load()
        return self.health()

    # ------------------------------------------------------------ 查询
    def all(self, include_disabled: bool = False) -> list[AgentManifest]:
        items = [m for m in self._agents.values() if include_disabled or m.enabled]
        return sorted(items, key=lambda m: (not m.default, m.id))

    def get(self, agent_id: str) -> AgentManifest | None:
        return self._agents.get(agent_id)

    def default_id(self) -> str:
        enabled = [m for m in self._agents.values() if m.enabled]
        if not enabled:
            return ""
        marked = [m for m in enabled if m.default]
        return (marked or sorted(enabled, key=lambda m: m.id))[0].id

    def errors(self) -> list[dict[str, str]]:
        return list(self._errors)

    # ------------------------------------------------------------ 路由
    def route(self, message: str, plan: dict | None = None) -> dict:
        """确定性优先的路由：概念解释类 → 默认 Agent；关键词（按长度加权）> query_plan.intent > 默认。

        为什么要单独判「概念解释」："安全库存的口径是什么" 这类问题要的是解释，不是诊断动作；
        若只按关键词匹配会被"安全库存"派给诊断 Agent，答非所问。这里用一条可解释的规则兜住：
        命中解释词且没有动作/症状词 → 直接给默认（通用）Agent。
        """
        text = message or ""
        if any(hint in text for hint in EXPLAIN_HINTS) and not any(
            hint in text for hint in ACTION_HINTS
        ):
            return {"agent_id": self.default_id(), "routed_by": "explain:default"}
        best_score, best_id, best_reason = 0, "", ""
        for manifest in self.all():
            for keyword in manifest.routing.keywords:
                if keyword and keyword in text:
                    score = len(keyword)
                    if score > best_score:
                        best_score, best_id, best_reason = score, manifest.id, f"keyword:{keyword}"
        if best_id:
            return {"agent_id": best_id, "routed_by": best_reason}
        intent = str((plan or {}).get("intent") or "")
        if intent:
            for manifest in self.all():
                if intent in manifest.routing.intents:
                    return {"agent_id": manifest.id, "routed_by": f"intent:{intent}"}
        return {"agent_id": self.default_id(), "routed_by": "default"}

    # ------------------------------------------------------------ 健康/API
    def health(self) -> dict:
        self.maybe_reload()
        return {
            "enabled": bool(getattr(get_settings(), "agents_enabled", True)),
            "dir": str(self.registry_dir()),
            "hot_reload": bool(getattr(get_settings(), "agents_hot_reload", True)),
            "count": len(self.all()),
            "total": len(self._agents),
            "disabled": [m.id for m in self.all(include_disabled=True) if not m.enabled],
            "default": self.default_id(),
            "agents": {m.id: {"version": m.version, "prompt_version": m.prompt_version,
                              "model": m.model or "", "enabled": m.enabled,
                              "max_tool_calls": m.max_tool_calls,
                              "stripped_write_tools": list(m.stripped_write_tools)}
                       for m in self.all(include_disabled=True)},
            "errors": self.errors(),
        }

    def public_list(self) -> list[dict[str, Any]]:
        self.maybe_reload()
        return [
            {
                "id": m.id,
                "name": m.name,
                "description": m.description,
                "version": m.version,
                "prompt": m.prompt,
                "prompt_version": m.prompt_version,
                "model": m.model or get_settings().llm_model,
                "temperature": m.temperature,
                "max_tokens": m.max_tokens,
                "max_tool_calls": m.max_tool_calls,
                "tools": m.tool_names,
                "stripped_write_tools": list(m.stripped_write_tools),
                "routing": {"keywords": m.routing.keywords, "intents": m.routing.intents},
                "enabled": m.enabled,
                "default": m.id == self.default_id(),
            }
            for m in self.all()
        ]


_registry: AgentRegistry | None = None


def get_registry() -> AgentRegistry:
    global _registry
    if _registry is None:
        _registry = AgentRegistry()
        _registry.load()
    return _registry


def reset_registry() -> None:
    """测试用：清掉单例（换目录后重新加载）。"""
    global _registry
    _registry = None
