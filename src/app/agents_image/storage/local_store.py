"""本地存储：`data/images/{refs,outputs,thumbnails}/<sha256>.<ext>`，**保留 24h** 后清理。

- `refs` = 中台商品图落地（v6 起用户不再上传，参考图一律来自中台）；
- 文件名用内容 sha256：同内容天然去重，不会重复占盘；
- 读取走鉴权接口（`/api/image/assets/{id}`），不把目录暴露出去；
- `sweep_expired` 只删本地存储且过期的文件（COS 走桶生命周期，不在代码里扫桶）。
"""

from __future__ import annotations

import datetime as dt
import pathlib

from app.agents_image.config import get_image_settings
from app.agents_image.storage.image_storage import AssetRef, image_size, sniff_mime
from app.core.artifacts import sha256_bytes
from app.core.errors import raise_image

_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
# kind → 子目录名（与 settings.*_dir 保持一致：refs / outputs / thumbnails）
_KIND_DIR = {"ref": "refs", "output": "outputs", "thumb": "thumbnails",
             "blocked": "blocked"}   # blocked：门禁不过的图（留档排查/人工放行）


class LocalStore:
    name = "local"

    def __init__(self) -> None:
        self.settings = get_image_settings()

    # ------------------------------------------------------------ 路径
    def _dir(self, kind: str) -> pathlib.Path:
        mapping = {"ref": self.settings.refs_dir,
                   "output": self.settings.outputs_dir,
                   "thumb": self.settings.thumbs_dir}
        directory = mapping.get(kind)
        if directory is None:
            raise_image("IMG_INPUT_INVALID", f"未知的图片类型：{kind}")
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _path(self, key: str) -> pathlib.Path:
        root = self.settings.images_dir.resolve()
        target = (root / key).resolve()
        if not str(target).startswith(str(root)):
            raise_image("IMG_INPUT_INVALID", f"非法的存储 key：{key}")
        return target

    # ------------------------------------------------------------ 读写
    def put(self, data: bytes, *, kind: str, mime: str) -> AssetRef:
        if not data:
            raise_image("IMG_REFERENCE_INVALID", "空文件")
        mime = (mime or sniff_mime(data)).lower()
        if mime not in _EXT:
            raise_image("IMG_REFERENCE_INVALID", f"仅支持 png/jpeg/webp，收到 {mime or '未知'}")
        digest = sha256_bytes(data)
        subdir = _KIND_DIR.get(kind)
        if subdir is None:
            raise_image("IMG_INPUT_INVALID", f"未知的图片类型：{kind}")
        key = f"{subdir}/{digest}.{_EXT[mime]}"
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(data)
        width, height = image_size(data)
        return AssetRef(key=key, sha256=digest, mime=mime, width=width, height=height,
                        bytes=len(data), storage=self.name)

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise_image("IMG_TASK_NOT_FOUND", f"文件不存在：{key}")
        return path.read_bytes()

    def signed_url(self, key: str, *, asset_id: str = "", ttl_seconds: int = 3600) -> str:
        """本地没有 CDN：返回本站鉴权读取接口（token 校验在 D4 接入）。"""
        if asset_id:
            return f"/api/image/assets/{asset_id}"
        return f"/api/image/assets/by-key/{key}"

    def delete(self, key: str) -> bool:
        path = self._path(key)
        if path.exists():
            path.unlink()
            return True
        return False

    def sweep_expired(self, hours: float) -> int:
        cutoff = dt.datetime.now() - dt.timedelta(hours=hours)
        removed = 0
        for kind_dir in (self.settings.refs_dir, self.settings.outputs_dir,
                         self.settings.thumbs_dir):
            if not kind_dir.exists():
                continue
            for path in kind_dir.glob("*"):
                if not path.is_file():
                    continue
                try:
                    mtime = dt.datetime.fromtimestamp(path.stat().st_mtime)
                except OSError:
                    continue
                if mtime < cutoff:
                    try:
                        path.unlink()
                        removed += 1
                    except OSError:  # pragma: no cover
                        continue
        return removed

    def health(self) -> dict:
        return {"ok": True, "backend": self.name,
                "root": str(self.settings.images_dir),
                "retention_hours": self.settings.retention_hours}
