from datetime import datetime, timedelta, timezone
import hashlib
from typing import Callable

from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import User

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
settings = get_settings()
ALGORITHM = "HS256"

# dj.aiwyq.bond cloud_vip 老用户迁移兼容：
# 存储格式 "djpbkdf2$<salt>$<pass_hash>"，pass_hash = pbkdf2_hmac(sha256, pwd, salt, 200000).hex()
# 无密码占位 "djpbkdf2$noopw"（纯 api_key/邀请/验证码登录，密码登录不可用）
_DJ_PBKDF2_PREFIX = "djpbkdf2$"
_DJ_PBKDF2_ROUNDS = 200000
_DJ_PBKDF2_NOPW = "noopw"


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def _verify_djpbkdf2(plain: str, hashed: str, update_hash: Callable[[str], None] | None) -> bool:
    parts = hashed.split("$", 2)  # ['djpbkdf2', salt, stored]
    if len(parts) != 3:
        return False
    salt, stored = parts[1], parts[2]
    if stored == _DJ_PBKDF2_NOPW or not salt:
        return False
    calc = hashlib.pbkdf2_hmac(
        "sha256", str(plain).encode("utf-8"), salt.encode("utf-8"), _DJ_PBKDF2_ROUNDS
    ).hex()
    ok = calc == stored
    if ok and update_hash is not None:
        update_hash(hash_password(plain))  # 惰性升级为 bcrypt
    return ok


def verify_password(plain: str, hashed: str, *, update_hash: Callable[[str], None] | None = None) -> bool:
    """多源兼容校验：本系统 $2b$、PHP $2y$、dj cloud_vip 迁移的 djpbkdf2$ 哈希。

    校验成功且 hashed 是旧源哈希（$2y$ 或 djpbkdf2$）时，调用 update_hash(新 $2b$ hash)
    惰性重哈希，实现「共用同一用户库」——各来源用户在本系统登录成功后平滑升级为 bcrypt。
    """
    if hashed.startswith(_DJ_PBKDF2_PREFIX):
        return _verify_djpbkdf2(plain, hashed, update_hash)
    ok = pwd_context.verify(plain, hashed)
    if ok and update_hash is not None and hashed.startswith("$2y$"):
        update_hash(hash_password(plain))
    return ok


def create_access_token(subject: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    return jwt.encode({"sub": subject, "exp": expire}, settings.secret_key, algorithm=ALGORITHM)


def decode_token(token: str) -> str | None:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
        sub = payload.get("sub")
        return str(sub) if sub else None
    except JWTError:
        return None


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def get_user_by_id(db: AsyncSession, user_id: int) -> User | None:
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()
