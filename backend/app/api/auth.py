# -*- coding: utf-8 -*-
"""auth.py — 本地 JWT 认证 + 156/dj 双身份源 + dj 8789 直连校验。

身份源顺序（登录）：
  1. 本地 users（bcrypt 直验）
  2. 156 standalone-auth（网页端/dj 注册用户回源，命中建号保留权益）
  3. dj 8789 /m/api/login（dj H5 注册用户直连，命中建号+权益快照+自动绑 key）
"""
from __future__ import annotations

import hashlib
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User, WalletLedger, UserOut
from app.services.auth import (
    create_access_token,
    get_current_user,
    get_user_by_email,
    hash_password,
    verify_password,
)
from app.services.standalone_log_report import report_auth_log, report_task_log

logger = logging.getLogger("app.api.auth")
router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6)
    nickname: str | None = Field(default=None, max_length=64)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str = Field(min_length=6)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    email: EmailStr
    token: str
    new_password: str = Field(min_length=6)


class MeUpdateRequest(BaseModel):
    nickname: str | None = Field(default=None, max_length=64)


# ---------------- dj.aiwyq.bond cloud_vip 登录互通（dj /m/ 注册用户自动建号） ----------------
# 与 /opt/cloud_vip/agnes_vip.py hash_password 完全同参：
# pbkdf2_hmac("sha256", pwd.encode(), salt.encode(), 200000).hex()
_DJ_PBKDF2_ROUNDS = 200000
_DJ_PBKDF2_PREFIX = "djpbkdf2$"


def _dj_pbkdf2_ok(password: str, salt: str, dj_hash: str) -> bool:
    return hashlib.pbkdf2_hmac(
        "sha256", str(password).encode("utf-8"), salt.encode("utf-8"), _DJ_PBKDF2_ROUNDS
    ).hex() == dj_hash


async def _provision_dj_user(db: AsyncSession, email: str, password: str) -> User | None:
    """dj.aiwyq.bond/m/ 注册用户首次登录 aidj 时，按 dj cloud_vip 只读自动建号。

    密码校验与 dj 侧同参 pbkdf2；命中则以 djpbkdf2$ 哈希懒迁移落本库
    （后续登录走本地 users 表），并套用与注册一致的赠送额度/体验金；
    dj 侧已开通的 Agnes 免费 key 一并自动绑定。
    未配置/库缺失/密码不符/占位无密码一律返回 None，绝不抛错。
    """
    cred = get_dj_credentials(email)
    if cred is None:
        return None
    salt, dj_hash, api_key = cred
    if not _dj_pbkdf2_ok(password, salt, dj_hash):
        return None
    settings = get_settings()
    grant = int(settings.billing_signup_grant_fen or 0)
    grant_days = int(getattr(settings, "billing_signup_grant_days", 7) or 0)
    grant_expire = datetime.now(UTC) + timedelta(days=grant_days) if grant > 0 and grant_days > 0 else None
    user = User(
        email=email,
        nickname="创作者",
        hashed_password=f"{_DJ_PBKDF2_PREFIX}{salt}${dj_hash}",
        quota_left=settings.new_user_quota,
        balance_fen=0,
        grant_fen=grant,
        frozen_grant_fen=0,
        grant_expire_at=grant_expire,
        plan="free",
    )
    db.add(user)
    await db.flush()
    if grant > 0:
        db.add(
            WalletLedger(
                user_id=user.id,
                delta_fen=grant,
                balance_after=grant,
                kind="grant",
                ref_type="signup",
                ref_id=str(user.id),
                note=f"signup_grant({getattr(settings, 'billing_signup_grant_days', 7)}d)",
            )
        )
    if api_key and user_api_keys.is_plausible_agnes_key(api_key):
        try:
            await user_api_keys.bind_agnes_key(db, user, api_key)
        except Exception:  # noqa: BLE001
            pass
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    email = body.email.lower()
    existing = await get_user_by_email(db, email)
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该邮箱已注册")

    settings = get_settings()
    grant = int(settings.billing_signup_grant_fen or 0)
    grant_days = int(getattr(settings, "billing_signup_grant_days", 7) or 0)
    grant_expire = datetime.now(UTC) + timedelta(days=grant_days) if grant > 0 and grant_days > 0 else None
    user = User(
        email=email,
        nickname=(body.nickname or "创作者")[:64],
        hashed_password=hash_password(body.password),
        quota_left=settings.new_user_quota,
        balance_fen=0,
        grant_fen=grant,
        frozen_grant_fen=0,
        grant_expire_at=grant_expire,
        plan="free",
    )
    db.add(user)
    await db.flush()
    if grant > 0:
        db.add(
            WalletLedger(
                user_id=user.id,
                delta_fen=grant,
                balance_after=grant,
                kind="grant",
                ref_type="signup",
                ref_id=str(user.id),
                note=f"signup_grant({grant_days}d)",
            )
        )
    await db.commit()
    await db.refresh(user)

    report_auth_log("register.success", email)  # fire-and-forget log registration
    return TokenResponse(access_token=create_access_token(str(user.id)))


async def _provision_remote_user(db: AsyncSession, email: str, password: str, info: dict) -> User:
    """156 账号校验通过后在本机建号（2026-10-18）。

    网页版（156）注册的账号可直接登录单机版：本机建同名账号、按返回的订阅/余额
    落一份 user_entitlements 快照（保留权益），并按 10 元 7 日规则注入本机赠额。
    密码用本机 bcrypt 重新哈希，后续登录只走本地校验。
    """
    from app.models_entitlement import UserEntitlement
    from app.services.remote_auth import entitlement_payload

    settings = get_settings()
    grant = int(settings.billing_signup_grant_fen or 0)
    grant_days = int(getattr(settings, "billing_signup_grant_days", 7) or 0)
    grant_expire = datetime.now(UTC) + timedelta(days=grant_days) if grant > 0 and grant_days > 0 else None
    user = User(
        email=email,
        nickname=(info.get("nickname") or email.split("@")[0])[:64],
        hashed_password=hash_password(password),
        quota_left=settings.new_user_quota,
        balance_fen=0,
        grant_fen=grant,
        frozen_grant_fen=0,
        grant_expire_at=grant_expire,
        plan="free",
    )
    db.add(user)
    await db.flush()
    if grant > 0:
        db.add(
            WalletLedger(
                user_id=user.id,
                delta_fen=grant,
                balance_after=grant,
                kind="grant",
                ref_type="signup",
                ref_id=str(user.id),
                note=f"signup_grant({grant_days}d)",
            )
        )
    payload = entitlement_payload(info)
    db.add(
        UserEntitlement(
            email=payload["email"],
            plan=payload["plan"],
            has_subscription=payload["has_subscription"],
            subscription_expires_at=payload["subscription_expires_at"],
            remote_balance_fen=payload["remote_balance_fen"],
            last_sync_error="",
            synced_at=payload["synced_at"],
            raw=payload["raw"],
        )
    )
    await db.commit()
    await db.refresh(user)
    return user


async def _provision_dj_8789_user(db: AsyncSession, email: str, password: str, info: dict) -> User:
    """dj 8789 账号校验通过后在本机建号（2026-10-18 第二通道）。

    与 156 回源通道（_provision_remote_user）对称：本机落一份 dj 账号权益快照，
    密码用本机 bcrypt 重哈希，后续登录走本地校验。dj 8789 侧的 api_key（Agnes
    免费 key，登录接口会返回）可用时一并自动绑定。
    """
    from app.models_entitlement import UserEntitlement
    from datetime import timezone

    settings = get_settings()
    grant = int(settings.billing_signup_grant_fen or 0)
    grant_days = int(getattr(settings, "billing_signup_grant_days", 7) or 0)
    grant_expire = datetime.now(UTC) + timedelta(days=grant_days) if grant > 0 and grant_days > 0 else None
    user = User(
        email=email,
        nickname=(info.get("nickname") or "创作者")[:64] or "创作者",
        hashed_password=hash_password(password),
        quota_left=settings.new_user_quota,
        balance_fen=0,
        grant_fen=grant,
        frozen_grant_fen=0,
        grant_expire_at=grant_expire,
        plan="free",
    )
    db.add(user)
    await db.flush()
    if grant > 0:
        db.add(
            WalletLedger(
                user_id=user.id,
                delta_fen=grant,
                balance_after=grant,
                kind="grant",
                ref_type="signup",
                ref_id=str(user.id),
                note=f"signup_grant({grant_days})dj8789",
            )
        )
    api_key = (info.get("api_key") or "").strip()
    if api_key and user_api_keys.is_plausible_agnes_key(api_key):
        try:
            await user_api_keys.bind_agnes_key(db, user, api_key)
        except Exception:  # noqa: BLE001
            pass
    db.add(
        UserEntitlement(
            email=email,
            plan="free",
            has_subscription=False,
            subscription_expires_at=None,
            remote_balance_fen=0,
            last_sync_error="",
            synced_at=datetime.now(timezone.utc),
            raw={"source": "dj_8789", "email": email},
        )
    )
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    email = body.email.lower()
    user = await get_user_by_email(db, email)
    if user is None:
        # dj.aiwyq.bond/m/ 注册用户首次登录：本库无号时按 dj cloud_vip 只读自动建号
        user = await _provision_dj_user(db, email, body.password)
    if user is None:
        # 156（aidj.aiwyq.bond）账号互通：本地无号时回源校验，命中即建号并保留权益
        from app.services.remote_auth import verify_remote_credentials

        info = await verify_remote_credentials(email, body.password)
        if info:
            user = await _provision_remote_user(db, email, body.password, info)
    if user is None:
        # dj 8789（dj.aiwyq.bond/m/）账号互通：本地+156 均无号时回 dj 校验，命中即建号
        from app.services.dj_8789_auth import dj_verify_credentials

        dj_info = await dj_verify_credentials(email, body.password)
        if dj_info:
            user = await _provision_dj_8789_user(db, email, body.password, dj_info)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="邮箱或密码错误")
    rehashed = False

    def _upgrade(new_hash: str) -> None:
        nonlocal rehashed
        user.hashed_password = new_hash
        rehashed = True

    if not verify_password(body.password, user.hashed_password, update_hash=_upgrade):
        # dj 互通兜底：本地 djpbkdf2$ 哈希（含 noopw 占位号 / dj 侧改密后）回源重校验，命中即懒补齐
        if user.hashed_password.startswith(_DJ_PBKDF2_PREFIX):
            cred = get_dj_credentials(body.email.lower())
            if cred and _dj_pbkdf2_ok(body.password, cred[0], cred[1]):
                user.hashed_password = f"{_DJ_PBKDF2_PREFIX}{cred[0]}${cred[1]}"
                rehashed = True
                verify_password(body.password, user.hashed_password, update_hash=_upgrade)
        if not verify_password(body.password, user.hashed_password):
            # 156 账号互通兜底：本地校验失败时回源校验（156 侧改密 / 首次同步场景）
            from app.services.remote_auth import verify_remote_credentials

            info = await verify_remote_credentials(email, body.password)
            if info:
                user.hashed_password = hash_password(body.password)
                rehashed = True
            else:
                # dj 8789 兜底：156 也不命中时回 dj 校验，命中即重哈希本机密码
                from app.services.dj_8789_auth import dj_verify_credentials

                dj_info = await dj_verify_credentials(email, body.password)
                if dj_info:
                    user.hashed_password = hash_password(body.password)
                    rehashed = True
                else:
                    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="邮箱或密码错误")
    if rehashed:
        await db.commit()
    report_auth_log("login.success", body.email)  # fire-and-forget log registration
    return TokenResponse(access_token=create_access_token(str(user.id)))


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> User:
    return user


@router.post("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, bool]:
    if not verify_password(body.old_password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="原密码错误")
    user.hashed_password = hash_password(body.new_password)
    await db.commit()
    return {"ok": True}


@router.post("/forgot-password")
async def forgot_password(body: ForgotPasswordRequest, db: AsyncSession = Depends(get_db)) -> dict[str, bool]:
    email = body.email.lower()
    user = await get_user_by_email(db, email)
    if user is None:
        return {"ok": True}  # 不泄露是否存在
    token = create_access_token(f"reset:{user.id}:{_secrets.token_hex(16)}", minutes=30)
    # SMTP_ENABLED=false 时不实际发送，仅落库（单机版零外部依赖）
    settings = get_settings()
    if settings.smtp_enabled:
        from app.services.mail import send_password_reset_mail
        await send_password_reset_mail(email, user.nickname or email, token, settings)
    return {"ok": True}


@router.post("/reset-password")
async def reset_password(
    body: ResetPasswordRequest, db: AsyncSession = Depends(get_db)
) -> dict[str, bool]:
    email = body.email.lower()
    user = await get_user_by_email(db, email)
    if user is None:
        return {"ok": True}
    user.hashed_password = hash_password(body.new_password)
    await db.commit()
    return {"ok": True}


@router.post("/me", response_model=UserOut)
async def update_me(
    body: MeUpdateRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> User:
    if body.nickname is not None:
        user.nickname = body.nickname[:64]
    await db.commit()
    await db.refresh(user)
    return user
