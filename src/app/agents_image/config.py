"""生图 Agent 独立配置（env 前缀 `IMAGE_`，读取同一个 `.env`）。

为什么单独一套 Settings：它要能在「只启用 image 模块」时独立启动，不能依赖 SKU 的
`app.config.Settings`；同时又复用 `.env`，所以 `env_file` 指向仓库根。
"""

from __future__ import annotations

import pathlib
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config import BASE_DIR, DATA_DIR

DEFAULT_IMAGE_AUTH_SECRET = "change-me-image-demo-secret"


class ImageSettings(BaseSettings):
    """生图 Agent 的全部开关；默认值 = 零成本本地开发态（fake 生图 + 本地存储 + 本地调度）。"""

    model_config = SettingsConfigDict(
        env_prefix="IMAGE_",
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    enabled: bool = True
    provider: str = "fake"            # fake | volcengine（后期验收门启用）
    storage: str = "local"            # local | cos
    dispatcher: str = "local"         # local | scf
    product_source: str = "fake"      # fake（开发/测试替身）| mcp（经 MCP 取中台数据）

    # ---- 商品与参考图（v6：用户只选商品，参考图来自中台；M3：按「商品+颜色」粒度）----
    max_refs: int = Field(default=6, ge=1, le=12)           # 每个「商品+颜色」的参考图合计上限
    refs_per_item: int = Field(default=2, ge=1, le=6)        # 每个颜色取几张（主图 + 细节）
    max_tasks_per_request: int = Field(default=60, ge=1, le=200)  # 颜色×平台×变体后的任务上限
    max_batch_products: int = Field(default=5, ge=1, le=10)   # 一次批量最多选几个商品
    max_total_images: int = Field(default=9, ge=1, le=9)
    # 需求描述长度上限（P2-6）：超长截断并落 IMG_PROMPT_TRUNCATED，避免把巨长文本塞给 provider
    max_prompt_chars: int = Field(default=500, ge=50, le=4000)
    concurrency: int = Field(default=8, ge=1, le=32)          # 本机任务并发
    provider_concurrency: int = Field(default=10, ge=1, le=64)  # 调第三方生图 API 的并发上限
    timeout_seconds: int = Field(default=60, ge=5, le=600)
    # 第三方生图 API 的重试（W4）：只对限流/超时/5xx 重试；每次重试都计一次「发起量」
    provider_retries: int = Field(default=2, ge=0, le=5)
    provider_retry_backoff_seconds: float = Field(default=0.2, ge=0.0, le=10.0)
    max_task_retries: int = Field(default=3, ge=0, le=10)  # 失败任务可被人工重试的次数上限
    # 商品上下文批量取数（W2）：同一 3 秒窗口内的请求合并成一次 MCP 调用
    context_batch_window_seconds: float = Field(default=3.0, ge=0.0, le=30.0)
    context_batch_max_ids: int = Field(default=50, ge=1, le=200)
    context_cache_ttl_seconds: float = Field(default=60.0, ge=0.0, le=600.0)
    recovery_interval_seconds: int = Field(default=30, ge=0, le=3600)  # 看门狗扫描间隔（0=关）
    # 任务租约（W1）：执行期每阶段续约；只有租约过期才允许看门狗重投
    task_lease_seconds: int = Field(default=300, ge=30, le=3600)
    max_recoveries: int = Field(default=3, ge=1, le=10)  # 同一任务最多被重投几次
    # 排队宽限（第三轮 P0-2）：queued 任务在这段时间内算「刚建、正在被提交」，看门狗不碰；
    # 超过宽限仍未被认领（没有 lease）才可能是孤儿任务
    claim_grace_seconds: int = Field(default=30, ge=0, le=600)

    # ---- 本地存储与清理（测试环境图片保留 24h）----
    images_root: str = ""             # 空 = data/images；测试/多实例时显式覆盖
    retention_hours: int = Field(default=24, ge=1, le=24 * 30)
    cleanup_interval_minutes: int = Field(default=60, ge=0, le=24 * 60)  # 0 = 不启动后台清理
    # outbox 自动重放（第三轮 P1-14）：回写失败的补偿队列由后台定时重放，不再只靠人工点
    outbox_replay_seconds: int = Field(default=60, ge=0, le=3600)
    outbox_replay_batch: int = Field(default=50, ge=1, le=500)
    # 第四轮 P1-5/P2-7：重放占位时长（防并发重发同一批）与已回写记录的保留时长
    outbox_claim_seconds: int = Field(default=120, ge=5, le=3600)
    outbox_keep_hours: int = Field(default=72, ge=1, le=24 * 30)

    # ---- 本地知识库（RAG：写法片段 / 修复经验，不提供参数与规则真相）----
    rag_enabled: bool = True
    # 第五轮 P1-1：默认 top_k 提到 3（1..8）；min_score 阈值默认 0（标定后再开，避免误杀召回）；
    # rerank 用启发式（标题命中 + 词覆盖），不引入 cross-encoder 依赖
    rag_top_k: int = Field(default=3, ge=1, le=8)
    rag_min_score: float = Field(default=0.0, ge=0.0, le=1.0)
    rag_rerank_backend: str = "heuristic"   # heuristic | none
    rag_kb_dir: str = ""              # 空 = data/kb_image
    # 修复经验（失败转成功才归档，P0-4）保留天数；过期由清理任务删除（人工样本不受影响）
    memory_keep_days: int = Field(default=90, ge=1, le=3650)

    # ---- 配额（按交付张数；管理员不受限）----
    default_quota_period: str = "day"       # day | week | month
    default_quota_images: int = Field(default=50, ge=0, le=100000)
    quota_period_key_rollover: bool = True

    # ---- 参考图三源（W3）：素材库 / 拍立淘 ----
    library_max_mb: float = Field(default=8.0, ge=0.1, le=50.0)   # 素材库单图上限（MB）
    library_max_images: int = Field(default=20, ge=1, le=200)     # 每人素材库上限（张）
    pailitao_min_sales: int = Field(default=100, ge=0, le=100000)  # 相似款销量门槛（>）
    pailitao_top_n: int = Field(default=5, ge=1, le=5)            # 每个商品最多取几个相似款

    # ---- 交付前像素门禁（M5）----
    compliance_gate: str = "main"          # main（只拦主图）| all（所有出图图位）| off
    compliance_autofix: bool = True        # 不过时先归白/裁方/放大再复检
    # 非 PNG（且没装 Pillow）时的像素门禁策略（第三轮 P1-4）：warn=交付但告警；block=按不通过处理
    compliance_nonpng: str = "warn"

    # ---- 质量与成本 ----
    critic_pass_score: int = Field(default=75, ge=0, le=100)
    reflexion_max_rounds: int = Field(default=2, ge=0, le=5)
    cost_per_image_cny: float = Field(default=0.0, ge=0.0)
    cost_budget_cny: float = Field(default=0.5, ge=0.0)

    # ---- 独立账号 ----
    auth_secret: str = DEFAULT_IMAGE_AUTH_SECRET
    token_ttl_hours: int = Field(default=8, ge=1, le=24 * 7)
    login_max_attempts: int = Field(default=8, ge=1, le=100)
    login_window_seconds: int = Field(default=300, ge=10, le=3600)

    # ---- 火山方舟（后期验收门启用；现在留空 → 自动用 fake）----
    ark_api_key: str = ""
    ark_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    ark_image_model: str = ""
    ark_vision_model: str = ""

    # ---- 中台 / MCP（D6 起使用）----
    platform_url: str = "http://127.0.0.1:8001"   # 数据中台（本仓库 data_platform，端口 8001）
    platform_token: str = ""                      # 空 = 回落到 PLATFORM_INTERNAL_TOKEN
    mcp_timeout_seconds: float = Field(default=5.0, ge=0.5, le=60)
    mcp_retries: int = Field(default=2, ge=0, le=5)

    @property
    def images_dir(self) -> pathlib.Path:
        return pathlib.Path(self.images_root) if self.images_root else pathlib.Path(DATA_DIR) / "images"

    @property
    def refs_dir(self) -> pathlib.Path:
        """中台商品图落地的参考图目录（与产出一同 24h 清理）。"""
        return self.images_dir / "refs"

    @property
    def outputs_dir(self) -> pathlib.Path:
        return self.images_dir / "outputs"

    @property
    def blocked_dir(self) -> pathlib.Path:
        """门禁不通过的图（留档排查/人工放行），参与 24h 清理。"""
        return self.images_dir / "blocked"

    @property
    def library_dir(self) -> pathlib.Path:
        """素材库（人工上传）落盘目录：不参与 24h 清理，由用户显式删除。"""
        return self.images_dir / "library"

    @property
    def thumbs_dir(self) -> pathlib.Path:
        return self.images_dir / "thumbnails"

    @property
    def is_fake_provider(self) -> bool:
        return self.provider.strip().lower() in {"", "fake"}

    @property
    def is_fake_catalog(self) -> bool:
        return self.product_source.strip().lower() in {"", "fake"}

    @property
    def platform_auth_token(self) -> str:
        """中台调用令牌：优先 `IMAGE_PLATFORM_TOKEN`，否则沿用中台的 `PLATFORM_INTERNAL_TOKEN`。"""
        import os

        return (self.platform_token or os.environ.get("PLATFORM_INTERNAL_TOKEN")
                or "demo-internal-token")

    @property
    def secret_is_default(self) -> bool:
        return self.auth_secret == DEFAULT_IMAGE_AUTH_SECRET


@lru_cache(maxsize=1)
def get_image_settings() -> ImageSettings:
    return ImageSettings()


def reset_image_settings() -> None:
    get_image_settings.cache_clear()
