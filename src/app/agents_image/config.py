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
    vision_provider: str = "fake"     # fake | volcengine
    storage: str = "local"            # local | cos
    dispatcher: str = "local"         # local | scf
    product_source: str = "fake"      # fake（开发/测试替身）| mcp（经 MCP 取中台数据）

    # ---- 商品与参考图（v6：用户只选商品，参考图来自中台）----
    max_refs: int = Field(default=6, ge=1, le=6)          # 每任务合计参考图上限
    max_batch_products: int = Field(default=5, ge=1, le=10)   # 一次批量最多选几个商品
    max_total_images: int = Field(default=9, ge=1, le=9)
    concurrency: int = Field(default=8, ge=1, le=32)
    timeout_seconds: int = Field(default=60, ge=5, le=600)

    # ---- 本地存储与清理（测试环境图片保留 24h）----
    images_root: str = ""             # 空 = data/images；测试/多实例时显式覆盖
    retention_hours: int = Field(default=24, ge=1, le=24 * 30)
    cleanup_interval_minutes: int = Field(default=60, ge=0, le=24 * 60)  # 0 = 不启动后台清理

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
