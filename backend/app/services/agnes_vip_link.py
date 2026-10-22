"""dj 站点（dj.aiwyq.bond）cloud_vip 的 Agnes 免费 key 同库直读。

aidj 与 cloud_vip 部署在同一台机器（156）。用户在 dj.aiwyq.bond/m/ 用某邮箱
开通 Agnes 后，dj 侧会把免费 key 写入 /opt/cloud_vip/cloud.db 的 agnes_vip 表。
aidj 后端在用户注册时按相同 email 只读查询该库，把 key 自动绑定到
user_api_keys（source='agnes'）。本模块**只读** cloud.db，绝不写该库。

未配置 AGNES_VIP_DB_PATH 或文件不存在时 is_available() 为 False，
注册流程静默降级（用户可稍后在 设置→API 手动「同步」）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.config import get_settings

# cloud_vip/cloud.db 固定结构（dj 侧 agnes_vip.py 维护）
_VIP_TABLE = "agnes_vip"


def _db_path() -> str | None:
    return (get_settings().agnes_vip_db_path or "").strip() or None


def is_available() -> bool:
    """配置了路径且文件可读。"""
    p = _db_path()
    if not p:
        return False
    return Path(p).is_file()


def get_agnes_key_for_email(email: str) -> str | None:
    """按 email 取 cloud_vip 中该用户已开通的 Agnes 免费 key（最新绑定优先）。

    返回 None：未配置、文件缺失、该邮箱尚未在 dj 侧开通或 key 尚未生成。
    """
    p = _db_path()
    if not p or not Path(p).is_file():
        return None
    email = (email or "").lower().strip()
    if not email:
        return None
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute(
                f"SELECT api_key FROM {_VIP_TABLE} "
                "WHERE LOWER(email)=? AND api_key IS NOT NULL AND api_key != '' "
                "ORDER BY COALESCE(updated_at, 0) DESC, created_at DESC LIMIT 1",
                (email,),
            ).fetchone()
            return (row[0] or "").strip() or None if row else None
        finally:
            conn.close()
    except sqlite3.Error:
        # 表结构缺失/锁超时：降级为无 key，不影响注册主流程
        return None


def get_dj_credentials(email: str) -> tuple[str, str, str] | None:
    """按 email 取 dj cloud_vip 侧的 (salt, pass_hash, api_key)。

    用于 aidj 登录时为 dj.aiwyq.bond/m/ 注册用户自动建号：dj 侧密码为
    pbkdf2_hmac(sha256, pwd, salt, 200000).hex()，salt/pass_hash 原样返回，
    由调用方用同参校验。占位行（无密码）或未配置时返回 None。
    本函数只读 cloud.db，绝不写该库。
    """
    p = _db_path()
    if not p or not Path(p).is_file():
        return None
    email = (email or "").lower().strip()
    if not email:
        return None
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
        try:
            row = conn.execute(
                f"SELECT salt, pass_hash, api_key FROM {_VIP_TABLE} "
                "WHERE LOWER(email)=?",
                (email,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if not row:
        return None
    salt = (row[0] or "").strip()
    ph = (row[1] or "").strip()
    if not salt or not ph:
        return None
    return salt, ph, (row[2] or "").strip()
