"""用户 API Key 创建、校验与撤销。"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.models_api import UserApiKey

API_KEY_PREFIX = "pf_live_"


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_api_key() -> tuple[str, str, str]:
    """返回 (完整 key, 展示前缀, hash)。"""
    secret = secrets.token_urlsafe(24).replace("-", "").replace("_", "")[:32]
    raw = f"{API_KEY_PREFIX}{secret}"
    prefix = raw[:16]
    return raw, prefix, _hash_key(raw)


async def create_api_key(db: AsyncSession, user: User, name: str) -> tuple[UserApiKey, str]:
    raw, prefix, key_hash = generate_api_key()
    row = UserApiKey(
        user_id=user.id,
        name=(name or "默认 Key").strip()[:64] or "默认 Key",
        key_prefix=prefix,
        key_hash=key_hash,
    )
    db.add(row)
    await db.flush()
    return row, raw


async def list_api_keys(db: AsyncSession, user_id: int) -> list[UserApiKey]:
    result = await db.execute(
        select(UserApiKey)
        .where(UserApiKey.user_id == user_id, UserApiKey.revoked_at.is_(None))
        .order_by(UserApiKey.id.desc())
    )
    return list(result.scalars().all())


async def revoke_api_key(db: AsyncSession, user_id: int, key_id: int) -> bool:
    row = await db.get(UserApiKey, key_id)
    if not row or row.user_id != user_id or row.revoked_at is not None:
        return False
    row.revoked_at = datetime.now(timezone.utc)
    await db.flush()
    return True


async def user_from_api_key(db: AsyncSession, raw_key: str) -> User | None:
    if not raw_key.startswith(API_KEY_PREFIX):
        return None
    key_hash = _hash_key(raw_key.strip())
    result = await db.execute(
        select(UserApiKey, User)
        .join(User, User.id == UserApiKey.user_id)
        .where(UserApiKey.key_hash == key_hash, UserApiKey.revoked_at.is_(None))
    )
    row = result.first()
    if not row:
        return None
    api_key, user = row
    api_key.last_used_at = datetime.now(timezone.utc)
    await db.flush()
    return user


# ---- Agnes 免费 key（dj 站点 cloud_vip 直读） ----
# user_api_keys 同时承载两种密钥：source='user'（pf_live_ 鉴权密钥，只存 hash）
# 与 source='agnes'（用户在 dj.aiwyq.bond 开通的 Agnes 平台免费 key，明文可查）。


def _key_prefix_of(raw: str) -> str:
    return raw[:16]


async def get_agnes_api_key(db: AsyncSession, user_id: int) -> UserApiKey | None:
    """取该用户最新一条未撤销的 source='agnes' 记录。"""
    result = await db.execute(
        select(UserApiKey)
        .where(UserApiKey.user_id == user_id, UserApiKey.source == "agnes", UserApiKey.revoked_at.is_(None))
        .order_by(UserApiKey.id.desc())
    )
    return result.scalars().first()


async def unbind_agnes_key(db: AsyncSession, user_id: int) -> bool:
    """解绑：撤销该用户最新的 source='agnes' 记录。"""
    row = await get_agnes_api_key(db, user_id)
    if not row:
        return False
    row.revoked_at = datetime.now(timezone.utc)
    await db.flush()
    return True


async def bind_agnes_key(db: AsyncSession, user: User, raw_key: str) -> UserApiKey | None:
    """绑定/刷新该用户的 Agnes 免费 key（同用户只保留一条 agnes 记录）。

    复用该用户已有的 agnes 行（含已撤销的）并复活，避免 key_hash 全局唯一约束
    与已撤销旧行冲突导致 IntegrityError。失败时回滚并返回 None，绝不抛错影响注册主流程。
    """
    raw_key = (raw_key or "").strip()
    if not raw_key:
        return None
    key_hash = _hash_key(f"agnes|{user.id}|{raw_key}")
    # 该用户最新的 agnes 行（不过滤撤销）：有则原地更新并复活，无则新建
    result = await db.execute(
        select(UserApiKey)
        .where(UserApiKey.user_id == user.id, UserApiKey.source == "agnes")
        .order_by(UserApiKey.id.desc())
    )
    existing = result.scalars().first()
    if existing:
        existing.plain_key = raw_key
        existing.key_prefix = _key_prefix_of(raw_key)
        existing.key_hash = key_hash
        existing.revoked_at = None
    else:
        row = UserApiKey(
            user_id=user.id,
            name="Agnes 免费 Key",
            source="agnes",
            plain_key=raw_key,
            key_prefix=_key_prefix_of(raw_key),
            key_hash=key_hash,
        )
        db.add(row)
        existing = row
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        return None
    return existing


AGNES_KEY_RE = re.compile(r"^(?:sk|cpk)-[A-Za-z0-9_\-]{8,128}$")


def is_plausible_agnes_key(raw: str) -> bool:
    return bool(AGNES_KEY_RE.match((raw or "").strip()))
