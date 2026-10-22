# -*- coding: utf-8 -*-
"""七牛 Kodo 存储驱动。

与 oss.py（阿里云）保持对称的公共方法面，由 oss.py 内部按 storage_provider 分发，
调用方（storage.py / drama/assets.py）无需改动：

- qiniu_enabled()
- qiniu_public_base() / qiniu_public_url(key)
- qiniu_upload_file / qiniu_upload_bytes / qiniu_upload_fileobj / qiniu_upload_dir
- reset_qiniu_client()

object key 布局沿用 {oss_folder}/... （与阿里云一致），FFmpeg 仍读本地文件。
"""
from __future__ import annotations

import logging
import mimetypes
from functools import lru_cache
from io import BufferedIOBase
from pathlib import Path

from app.config import get_settings

logger = logging.getLogger(__name__)


@lru_cache
def _auth_and_region():
    import qiniu
    from qiniu import Region

    s = get_settings()
    if not (s.qiniu_access_key and s.qiniu_secret_key):
        raise RuntimeError("Qiniu credentials incomplete (qiniu_access_key/qiniu_secret_key)")
    auth = qiniu.Auth(s.qiniu_access_key, s.qiniu_secret_key)
    # region 缺省 auto：SDK 自动探测上传区域；可配置 east/north/south/us/as0/as1
    region = Region.from_region_id((s.qiniu_region or "auto").strip() or "auto")
    return auth, region


def reset_qiniu_client() -> None:
    """后台改七牛密钥/区域后清缓存，避免继续用旧凭据。"""
    _auth_and_region.cache_clear()


def qiniu_enabled() -> bool:
    s = get_settings()
    return bool(
        s.oss_enabled
        and s.storage_provider.strip().lower() in {"qiniu", "qiniu_kodo", "qiniukodo"}
        and s.qiniu_bucket
        and s.qiniu_access_key
        and s.qiniu_secret_key
    )


def _bucket() -> str:
    return get_settings().qiniu_bucket.strip()


def qiniu_public_base() -> str:
    """浏览器可访问的公网基址。"""
    s = get_settings()
    if s.qiniu_public_base.strip():
        return s.qiniu_public_base.rstrip("/")
    # 未配 CDN 域名时退化为默认测试域名（生产务必配 qiniu_public_base）
    return f"https://{s.qiniu_bucket.strip()}.qiniudn.com"


def qiniu_public_url(object_key: str) -> str:
    key = object_key.lstrip("/")
    return f"{qiniu_public_base()}/{key}"


def _mime(path_name: str) -> str:
    return mimetypes.guess_type(path_name)[0] or "application/octet-stream"


def _ret_key(ret: object) -> str:
    """SDK 不同版本 put_* 返回 dict 或 Ret 对象，统一取 key。"""
    if isinstance(ret, dict):
        return str(ret.get("key") or "")
    return str(getattr(ret, "key", "") or "")


def qiniu_upload_file(local_path: Path, object_key: str) -> str:
    """object_key 由调用方（oss.py 分发层）按统一 key 布局算好传入。"""
    import qiniu

    path = Path(local_path)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    key = object_key.lstrip("/")
    auth, region = _auth_and_region()
    up_token = auth.upload_token(_bucket(), key)
    ret, resp = qiniu.put_file_v2(up_token, key, str(path), mime_type=_mime(path.name), regions=[region])
    got = _ret_key(ret)
    if got != key:
        raise RuntimeError(f"qiniu upload failed: {getattr(resp, 'status_code', None)} {getattr(resp, 'error', '')} got_key={got!r}")
    url = qiniu_public_url(key)
    logger.info("qiniu uploaded %s → %s", path.name, url)
    return url


def qiniu_upload_bytes(data: bytes, object_key: str, *, content_type: str = "application/octet-stream") -> str:
    import qiniu

    key = object_key.lstrip("/")
    auth, region = _auth_and_region()
    up_token = auth.upload_token(_bucket(), key)
    ret, resp = qiniu.put_data(up_token, key, data, mime_type=content_type, regions=[region])
    got = _ret_key(ret)
    if got != key:
        raise RuntimeError(f"qiniu upload failed: {getattr(resp, 'status_code', None)} {getattr(resp, 'error', '')} got_key={got!r}")
    return qiniu_public_url(key)


def _stream_size(fileobj: BufferedIOBase) -> int:
    """尽力取流大小（put_stream 需要）；seek 失败返回 0（调用方回退 put_data）。"""
    import io

    try:
        start = fileobj.tell()
        fileobj.seek(0, 2)
        size = fileobj.tell() - start
        fileobj.seek(start)
        return max(int(size), 0)
    except (OSError, AttributeError, io.UnsupportedOperation):
        return 0


def qiniu_upload_fileobj(fileobj: BufferedIOBase, object_key: str, *, content_type: str = "application/octet-stream") -> str:
    import qiniu

    key = object_key.lstrip("/")
    auth, region = _auth_and_region()
    up_token = auth.upload_token(_bucket(), key)
    size = _stream_size(fileobj)
    if size > 0:
        ret, resp = qiniu.put_stream(
            up_token, key, fileobj, file_name=object_key.lstrip("/").split("/")[-1],
            data_size=size, mime_type=content_type, regions=[region],
        )
        got = _ret_key(ret)
        if got != key:
            raise RuntimeError(f"qiniu upload failed: {getattr(resp, 'status_code', None)} {getattr(resp, 'error', '')} got_key={got!r}")
        return qiniu_public_url(key)
    # 无法预知大小（管道/网络流）：整段读入内存走 put_data
    data = fileobj.read()
    ret, resp = qiniu.put_data(up_token, key, data, mime_type=content_type, regions=[region])
    got = _ret_key(ret)
    if got != key:
        raise RuntimeError(f"qiniu upload failed: {getattr(resp, 'status_code', None)} {getattr(resp, 'error', '')} got_key={got!r}")
    return qiniu_public_url(key)


def qiniu_upload_dir(local_dir: Path, qiniu_prefix: str) -> int:
    root = Path(local_dir)
    if not root.is_dir():
        raise NotADirectoryError(str(root))
    prefix = qiniu_prefix.strip().strip("/")
    count = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        key = f"{prefix}/{path.relative_to(root).as_posix()}"
        qiniu_upload_file(path, key)
        count += 1  # noqa: E702 (保持与 aliyun 版同构)
    return count


def ensure_qiniu_cors() -> None:
    """七牛/CDN 的跨域在控制台配置（Bucket 策略 + CDN CORS），代码侧无 per-object ACL。

    上传时默认 public-read 需 bucket 设为公共读；这里仅提示，不抛错。
    """
    logger.info("qiniu CORS/ACL configured in Qiniu console (bucket public-read + CDN); nothing to do in app")
