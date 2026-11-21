"""worker 侧的存储后端：内存（测试）/ 腾讯云 COS / 数据中台文件夹。

为什么 worker 自带一份而不是复用 `app/agents_image/storage`：
- worker 要能单独打进 SCF 包，不能顺着 `storage/__init__.py` 拉起本地存储与 SKU 配置；
- 落点语义也不同：父进程的 `ImageStorage` 负责"本地 24h 留档"，worker 负责
  "把产物放到 COS / 中台指定文件夹并把路径交回去"。

三个后端都实现同一组方法：
`put(data, *, kind, mime, task_id, slot, prefix) -> StoredObject`、`get(key) -> bytes`、
`health() -> dict`。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any

from app.core.errors import ImageError, raise_image

logger = logging.getLogger("sku-agent.image.worker.storage")

_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data or b"").hexdigest()


def sniff_mime(data: bytes) -> str:
    if (data or b"").startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if (data or b"").startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if (data or b"")[:4] == b"RIFF" and (data or b"")[8:12] == b"WEBP":
        return "image/webp"
    return ""


def image_size(data: bytes) -> tuple[int, int]:
    from app.agents_image.storage.image_storage import image_size as _size

    return _size(data)


@dataclass
class StoredObject:
    storage: str
    key: str
    sha256: str
    mime: str
    width: int = 0
    height: int = 0
    bytes: int = 0
    url: str = ""
    url_expire_at: str = ""


def object_key(*, prefix: str, task_id: str, slot: int, kind: str, sha256: str,
               mime: str) -> str:
    """对象 key 规范：`<prefix><blocked?><task_id>/slot<slot>-<sha12>.<ext>`。

    含 sha 前缀 → 同一内容重复上传覆盖同一 key（幂等）；blocked 图单独前缀便于排查。
    """
    ext = _EXT.get((mime or "").lower(), "png")
    folder = f"{prefix or ''}{'blocked/' if kind == 'blocked' else ''}{task_id or 'unknown'}"
    return f"{folder}/slot{int(slot)}-{sha256[:12]}.{ext}"


class MemoryStorage:
    """进程内存储（测试 / 本地演示；进程退出即丢）。"""

    name = "memory"

    def __init__(self) -> None:
        self._objects: dict[str, tuple[bytes, str]] = {}

    def put(self, data: bytes, *, kind: str = "output", mime: str = "",
            task_id: str = "", slot: int = 0, prefix: str = "",
            ttl_seconds: int = 3600) -> StoredObject:
        if not data:
            raise_image("IMG_STORAGE_UNAVAILABLE", "空内容不能落存储")
        mime = (mime or sniff_mime(data)).lower() or "image/png"
        digest = sha256_bytes(data)
        key = object_key(prefix=prefix, task_id=task_id, slot=slot, kind=kind,
                         sha256=digest, mime=mime)
        self._objects[key] = (data, mime)
        width, height = image_size(data)
        return StoredObject(storage=self.name, key=key, sha256=digest, mime=mime,
                            width=width, height=height, bytes=len(data),
                            url=f"memory://{key}", url_expire_at="")

    def get(self, key: str) -> bytes:
        hit = self._objects.get(str(key or ""))
        if hit is None:
            raise_image("IMG_REFERENCE_EXPIRED", f"存储对象不存在或已过期：{key}")
        return hit[0]

    def health(self) -> dict:
        return {"ok": True, "backend": self.name, "objects": len(self._objects)}


class CosStorage:
    """腾讯云 COS：用官方 SDK 落对象，读用 `get_object`，URL 用预签名/公开前缀。

    SDK 是**惰性导入**：没装/没配凭据时只在该后端被选中时才报错，
    不影响本地开发与离线测试（默认后端不是 cos）。
    """

    name = "cos"

    def __init__(self, *, secret_id: str = "", secret_key: str = "", region: str = "",
                 bucket: str = "", prefix: str = "", public_base: str = "",
                 signed_ttl_seconds: int = 3600) -> None:
        from app.agents_image.config import get_image_settings

        settings = get_image_settings()
        self.secret_id = (secret_id or settings.cos_secret_id
                          or _env("TENCENTCLOUD_SECRET_ID"))
        self.secret_key = (secret_key or settings.cos_secret_key
                           or _env("TENCENTCLOUD_SECRET_KEY"))
        self.region = region or settings.cos_region
        self.bucket = bucket or settings.cos_bucket
        self.prefix = prefix or settings.cos_prefix
        self.public_base = (public_base or settings.cos_public_base).rstrip("/")
        self.signed_ttl_seconds = int(signed_ttl_seconds or settings.cos_signed_ttl_seconds)
        if not (self.secret_id and self.secret_key and self.region and self.bucket):
            raise_image("IMG_STORAGE_UNAVAILABLE",
                        "COS 未配置：需要 IMAGE_COS_SECRET_ID / SECRET_KEY / REGION / BUCKET"
                        "（或 TENCENTCLOUD_SECRET_ID/KEY）")

    def _client(self):  # noqa: ANN202 —— 第三方客户端
        try:
            from qcloud_cos import CosConfig, CosS3Client  # type: ignore[import-not-found]
        except Exception as exc:  # noqa: BLE001 —— 没装 SDK
            raise_image("IMG_STORAGE_UNAVAILABLE",
                        f"未安装 COS SDK（pip install cos-python-sdk-v5）：{exc}")
        config = CosConfig(Region=self.region, SecretId=self.secret_id,
                           SecretKey=self.secret_key, Scheme="https")
        return CosS3Client(config)

    def put(self, data: bytes, *, kind: str = "output", mime: str = "",
            task_id: str = "", slot: int = 0, prefix: str = "",
            ttl_seconds: int = 0) -> StoredObject:
        if not data:
            raise_image("IMG_STORAGE_UNAVAILABLE", "空内容不能落存储")
        mime = (mime or sniff_mime(data)).lower() or "image/png"
        digest = sha256_bytes(data)
        key = object_key(prefix=prefix or self.prefix, task_id=task_id, slot=slot,
                         kind=kind, sha256=digest, mime=mime)
        try:
            self._client().put_object(Bucket=self.bucket, Body=data, Key=key,
                                      ContentType=mime)
            url, expire_at = self._url_for(key)
        except ImageError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 网络/鉴权/桶不存在
            raise_image("IMG_STORAGE_UNAVAILABLE",
                        f"COS 上传失败（{type(exc).__name__}）：{exc}", retryable=True)
        width, height = image_size(data)
        return StoredObject(storage=self.name, key=key, sha256=digest, mime=mime,
                            width=width, height=height, bytes=len(data),
                            url=url, url_expire_at=expire_at)

    def _url_for(self, key: str) -> tuple[str, str]:
        if self.public_base:
            return f"{self.public_base}/{key}", ""
        try:
            url = self._client().get_presigned_url(
                Bucket=self.bucket, Key=key, Expired=self.signed_ttl_seconds)
            expire = time.strftime("%Y-%m-%dT%H:%M:%S",
                                   time.gmtime(time.time() + self.signed_ttl_seconds))
            return url, expire
        except Exception:  # noqa: BLE001 —— 预签名失败就只回 key
            logger.warning("COS 预签名 URL 生成失败（key=%s）", key)
            return "", ""

    def get(self, key: str) -> bytes:
        try:
            resp = self._client().get_object(Bucket=self.bucket, Key=str(key or ""))
            body = resp["Body"]
            return body.get_raw_stream().read()
        except ImageError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise_image("IMG_REFERENCE_EXPIRED",
                        f"COS 读取失败（对象可能已过期/被清理）：{key}：{type(exc).__name__}")

    def health(self) -> dict:
        return {"ok": bool(self.bucket), "backend": self.name, "region": self.region,
                "bucket": self.bucket, "prefix": self.prefix}


class PlatformStorage:
    """数据中台"指定文件夹"：调中台新增的上传接口，返回中台给的 storage_key/签名 URL。"""

    name = "platform"

    def __init__(self, *, base_url: str = "", token: str = "", prefix: str = "",
                 timeout: float = 10.0, max_bytes: int = 0) -> None:
        from app.agents_image.config import get_image_settings

        settings = get_image_settings()
        self.base_url = (base_url or settings.platform_url).rstrip("/")
        self.token = token or settings.platform_auth_token
        self.prefix = prefix or settings.platform_store_prefix
        self.timeout = float(timeout)
        self.max_bytes = int(max_bytes or settings.result_max_mb * 1024 * 1024)

    async def _post(self, url: str, data: bytes, mime: str, headers: dict[str, str]) -> dict:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, content=data,
                                         headers={**headers, "Content-Type": mime})
        except Exception as exc:  # noqa: BLE001 —— 连接/超时
            raise_image("IMG_STORAGE_UNAVAILABLE",
                        f"中台存储不可用（{type(exc).__name__}）：{exc}", retryable=True)
        if resp.status_code >= 500:
            raise_image("IMG_STORAGE_UNAVAILABLE",
                        f"中台存储错误 HTTP {resp.status_code}", retryable=True)
        if resp.status_code >= 400:
            raise_image("IMG_STORAGE_UNAVAILABLE",
                        f"中台拒绝上传 HTTP {resp.status_code}：{resp.text[:200]}")
        try:
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise_image("IMG_STORAGE_UNAVAILABLE", f"中台返回不是 JSON：{exc}")
        return body if isinstance(body, dict) else {}

    async def put_async(self, data: bytes, *, kind: str = "output", mime: str = "",
                        task_id: str = "", slot: int = 0, prefix: str = "") -> StoredObject:
        if not data:
            raise_image("IMG_STORAGE_UNAVAILABLE", "空内容不能落存储")
        if len(data) > self.max_bytes:
            raise_image("IMG_STORAGE_UNAVAILABLE",
                        f"单图 {len(data)} 字节超过中台上限 {self.max_bytes}")
        mime = (mime or sniff_mime(data)).lower() or "image/png"
        digest = sha256_bytes(data)
        url = (f"{self.base_url}/api/v1/image/results/{task_id or 'adhoc'}/"
               f"{int(slot)}/binary")
        body = await self._post(url, data, mime, {
            "X-Internal-Token": self.token,
            "X-Agent-Id": "image-worker",
            "X-Image-Kind": kind,
            "X-Storage-Prefix": prefix or self.prefix,
            "X-Content-Sha256": digest,
        })
        width, height = image_size(data)
        url = str(body.get("url") or "")
        if url.startswith("/"):            # 中台返回的是相对路径 → 拼成 Agent 可直接访问的绝对 URL
            url = f"{self.base_url}{url}"
        return StoredObject(
            storage=self.name, key=str(body.get("storage_key") or ""), sha256=digest,
            mime=mime, width=int(body.get("width") or width),
            height=int(body.get("height") or height), bytes=len(data),
            url=url, url_expire_at=str(body.get("expire_at") or ""))

    def put(self, data: bytes, *, kind: str = "output", mime: str = "",
            task_id: str = "", slot: int = 0, prefix: str = "") -> StoredObject:
        """同步入口（脚本/测试用）：在一个独立事件循环里跑 `put_async`。"""
        import asyncio

        return asyncio.run(self.put_async(data, kind=kind, mime=mime, task_id=task_id,
                                          slot=slot, prefix=prefix))

    async def get_async(self, key: str) -> bytes:
        url = str(key or "")
        if not url.startswith("http"):
            # 传进来的是 storage_key（`results/<task>/slot...`）：走中台的结果资产读取接口，
            # 用 Bearer 令牌鉴权（前端走的是短签名 URL，见 `put_async` 返回的 url）。
            url = (f"{self.base_url}/api/v1/image/results/assets/"
                   f"{url.lstrip('/')}")
        import httpx

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(url, headers={"X-Internal-Token": self.token})
        except Exception as exc:  # noqa: BLE001
            raise_image("IMG_REFERENCE_EXPIRED", f"中台读取失败：{type(exc).__name__}")
        if resp.status_code >= 400:
            raise_image("IMG_REFERENCE_EXPIRED",
                        f"中台读取失败 HTTP {resp.status_code}：{key}")
        return resp.content

    def get(self, key: str) -> bytes:
        import asyncio

        return asyncio.run(self.get_async(key))

    def health(self) -> dict:
        return {"ok": True, "backend": self.name, "base_url": self.base_url,
                "prefix": self.prefix}


def _env(name: str) -> str:
    import os

    return str(os.environ.get(name) or "").strip()


_REGISTRY = {"memory": MemoryStorage, "cos": CosStorage, "platform": PlatformStorage}
# 进程内共享的 memory 后端：父进程上传参考图 → worker 读同一个 dict（本地/测试链路）
_MEMORY_SINGLETON: dict[str, MemoryStorage] = {}


def get_storage(backend: str = "") -> Any:
    """按名字取 worker 侧存储后端；`backend` 空 = `IMAGE_WORKER_STORAGE`/`IMAGE_STORAGE`。"""
    from app.agents_image.config import get_image_settings

    settings = get_image_settings()
    name = (backend or settings.effective_worker_storage or "memory").strip().lower()
    if name == "local":
        # 本地磁盘只是父进程的留档后端；worker 侧本地=内存（要持久化请用 cos/platform）
        name = "memory"
    factory = _REGISTRY.get(name)
    if factory is None:
        raise_image("IMG_STORAGE_UNAVAILABLE",
                    f"worker 不支持的存储后端：{name}（可用：{'、'.join(sorted(_REGISTRY))}）")
    if name == "memory":
        return _MEMORY_SINGLETON.setdefault("memory", MemoryStorage())
    return factory()


def encode_for_transport(data: bytes) -> str:
    """`return_bytes=True` 时把字节放进响应（base64，仅调试/本地用）。"""
    return base64.b64encode(data or b"").decode("ascii")


__all__ = ["StoredObject", "MemoryStorage", "CosStorage", "PlatformStorage",
           "get_storage", "object_key", "sha256_bytes", "sniff_mime", "image_size",
           "encode_for_transport"]
