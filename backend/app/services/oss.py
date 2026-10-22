"""对象存储上传助手（按 storage_provider 分发：aliyun / qiniu）。

Local files stay on disk for FFmpeg; DB / frontend use public OSS URLs.
Object key layout: {folder}/generated/p{id}/... and {folder}/ for SPA.

storage_provider=aliyun（默认）→ 阿里云 OSS（oss2）；
storage_provider=qiniu → 七牛 Kodo（app/services/qiniu.py），
公共方法面保持一致，调用方（storage.py / oss_queue.py / assets.py）无需改动。
"""

from __future__ import annotations

import logging
import mimetypes
from functools import lru_cache
from io import BufferedIOBase
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)


def _is_qiniu() -> bool:
    return get_settings().storage_provider.strip().lower() in {
        "qiniu",
        "qiniu_kodo",
        "qiniukodo",
    }


def _is_local() -> bool:
    """storage_provider=local：不出云，文件全部留在本地 alipan。"""
    return get_settings().storage_provider.strip().lower() in {"local", "none", "localdisk"}

def _qiniu():
    """懒加载七牛驱动，避免循环导入。"""
    from app.services import qiniu as qiniu_svc

    return qiniu_svc


@lru_cache
def _bucket():
    import oss2

    s = get_settings()
    if not s.oss_enabled:
        raise RuntimeError("OSS disabled")
    if not (s.oss_bucket and s.oss_access_key_id and s.oss_access_key_secret):
        raise RuntimeError("OSS credentials incomplete")
    auth = oss2.Auth(s.oss_access_key_id, s.oss_access_key_secret)
    endpoint = s.oss_endpoint.strip()
    if not endpoint.startswith("http"):
        endpoint = f"https://{endpoint}"
    return oss2.Bucket(auth, endpoint, s.oss_bucket)


def reset_oss_client() -> None:
    """后台改 OSS/七牛密钥后丢掉缓存客户端，避免继续用已禁用的旧 Key。"""
    _bucket.cache_clear()
    try:
        _qiniu().reset_qiniu_client()
    except Exception:  # noqa: BLE001
        pass


def oss_enabled() -> bool:
    s = get_settings()
    if _is_local():
        return False
    if _is_qiniu():
        return _qiniu().qiniu_enabled()
    return bool(
        s.oss_enabled
        and s.oss_bucket
        and s.oss_access_key_id
        and s.oss_access_key_secret
    )


def folder_prefix() -> str:
    return get_settings().oss_folder.strip().strip("/") or "kepu"


def public_base() -> str:
    """浏览器可访问的公网基址；内网上传 endpoint 不能出现在返回 URL 里。"""
    if _is_local():
        return get_settings().public_base_url.rstrip('/') + '/qnm'
    if _is_qiniu():
        return _qiniu().qiniu_public_base()
    s = get_settings()
    if s.oss_public_base.strip():
        return s.oss_public_base.rstrip("/")
    ep = s.oss_endpoint.strip().removeprefix("https://").removeprefix("http://")
    ep = ep.replace("-internal.aliyuncs.com", ".aliyuncs.com")
    ep = ep.replace("-internal-accelerate.aliyuncs.com", ".aliyuncs.com")
    return f"https://{s.oss_bucket}.{ep}"


def public_url(object_key: str) -> str:
    key = object_key.lstrip("/")
    return f"{public_base()}/{key}"


def ensure_browser_cors() -> None:
    """Allow SPA origins to fetch OSS media (needed for client-side zip)."""
    if not oss_enabled():
        return
    if _is_qiniu():
        _qiniu().ensure_qiniu_cors()
        return
    s = get_settings()
    origins = [
        o.strip()
        for o in (s.cors_origins or "").split(",")
        if o.strip()
    ]
    # Production site + common local/dev（主站 www 优先）
    for extra in (
        "https://www.printfilm.com",
        "http://www.printfilm.com",
        "https://printfilm.com",
        "http://printfilm.com",
        "https://admin.printfilm.com",
        "http://admin.printfilm.com",
        "https://kepu.printfilm.com",
        "http://kepu.printfilm.com",
        "https://admin.kepu.printfilm.com",
        "https://kepu.printtfilm.com",
        "http://kepu.printtfilm.com",
        "https://admin.kepu.printtfilm.com",
        "http://admin.kepu.printtfilm.com",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
    ):
        if extra not in origins:
            origins.append(extra)
    pub = (s.public_base_url or "").rstrip("/")
    if pub and pub not in origins:
        origins.append(pub)
    try:
        import oss2
        from oss2.models import BucketCors, CorsRule

        rule = CorsRule(
            allowed_origins=origins,
            allowed_methods=["GET", "HEAD"],
            allowed_headers=["*"],
            expose_headers=["ETag", "Content-Length", "Content-Type"],
            max_age_seconds=3600,
        )
        _bucket().put_bucket_cors(BucketCors([rule]))
        logger.info("oss CORS updated for %s origin(s)", len(origins))
    except Exception:  # noqa: BLE001
        logger.exception("oss CORS update failed")


def key_for_local(path: Path, *, static_root: Path) -> str:
    """Map backend/static/... → {folder}/..."""
    p2 = Path(path)
    root = Path(static_root)
    rel = None
    for p, r in ((p2, root), (p2.absolute(), root.absolute())):
        try:
            rel = p.relative_to(r).as_posix()
            break
        except ValueError:
            pass
    if rel is None:
        resolved = p2.resolve()
        gen = (root / "generated").resolve()
        try:
            rel = "generated/" + resolved.relative_to(gen).as_posix()
        except ValueError:
            rel = resolved.relative_to(root.resolve()).as_posix()
    return f"{folder_prefix()}/{rel}"


def upload_file(local_path: Path, object_key: str | None = None) -> str:
    """Upload local file; return public URL. Raises on failure."""
    from app.services import storage

    path = Path(local_path)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    key = (object_key or key_for_local(path, static_root=storage.STATIC_ROOT)).lstrip("/")
    if _is_local():
        logger.info("local provider: %s -> %s (no cloud upload)", key, public_url(key))
        return public_url(key)
    if _is_qiniu():
        return _qiniu().qiniu_upload_file(path, key)
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    headers = {
        "Content-Type": mime,
        "x-oss-object-acl": "public-read",
    }
    bucket = _bucket()
    bucket.put_object_from_file(key, str(path), headers=headers)
    url = public_url(key)
    logger.info("oss uploaded %s → %s", path.name, url)
    return url


def upload_bytes(data: bytes, object_key: str, *, content_type: str = "application/octet-stream") -> str:
    key = object_key.lstrip("/")
    if _is_local():
        from app.services import storage as _st
        _fp = folder_prefix()
        _rel = key[len(_fp) + 1:] if key.startswith(_fp + '/') else key
        if not _rel.startswith('generated/'):
            _rel = 'generated/' + _rel
        _dest = _st.STATIC_ROOT / _rel
        _dest.parent.mkdir(parents=True, exist_ok=True)
        _dest.write_bytes(data)
        logger.info("local upload_bytes -> %s (%d bytes)", _dest, len(data))
        return public_url(key)
    if _is_qiniu():
        return _qiniu().qiniu_upload_bytes(data, key, content_type=content_type)
    headers = {"Content-Type": content_type, "x-oss-object-acl": "public-read"}
    _bucket().put_object(key, data, headers=headers)
    return public_url(key)


# 直接将文件对象流式上传到 OSS，避免先整文件读入内存。
def upload_fileobj(fileobj: BufferedIOBase, object_key: str, *, content_type: str = "application/octet-stream") -> str:
    key = object_key.lstrip("/")
    if _is_local():
        from app.services import storage as _st
        _fp = folder_prefix()
        _rel = key[len(_fp) + 1:] if key.startswith(_fp + '/') else key
        if not _rel.startswith('generated/'):
            _rel = 'generated/' + _rel
        _data = fileobj.read()
        _dest = _st.STATIC_ROOT / _rel
        _dest.parent.mkdir(parents=True, exist_ok=True)
        _dest.write_bytes(_data)
        logger.info("local upload_fileobj -> %s (%d bytes)", _dest, len(_data))
        return public_url(key)
    if _is_qiniu():
        return _qiniu().qiniu_upload_fileobj(fileobj, key, content_type=content_type)
    headers = {"Content-Type": content_type, "x-oss-object-acl": "public-read"}
    _bucket().put_object(key, fileobj, headers=headers)
    return public_url(key)


def upload_dir(local_dir: Path, oss_prefix: str) -> int:
    """Upload directory recursively under oss_prefix. Returns file count."""
    root = Path(local_dir)
    if not root.is_dir():
        raise NotADirectoryError(str(root))
    if _is_local():
        logger.info("local provider: upload_dir no-op (files stay local), prefix=%s", oss_prefix)
        return 0
    if _is_qiniu():
        return _qiniu().qiniu_upload_dir(root, oss_prefix)
    prefix = oss_prefix.strip().strip("/")
    count = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        key = f"{prefix}/{rel}"
        upload_file(path, key)
        count += 1
    return count
