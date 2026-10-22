# -*- coding: utf-8 -*-
"""单机版 → 156 账号校验（远程登录互通）。

设计（2026-10-18）：
- 156 侧 /prod-api/standalone-auth 用 STANDALONE_LOG_KEY 鉴权，按 email+password
  校验该账号在 156 的密码（bcrypt / djpbkdf2 多源兼容），返回订阅与余额。
- 本机登录本地查无此号或密码不匹配时回源校验：命中即在本机建号并保留权益，
  实现「同一套账号，网页版与单机版都可登录」。
- 只读校验，不修改 156 侧任何账号数据；156 不可达时静默降级为本地校验结果。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from app.config import get_settings

logger = logging.getLogger("app.remote_auth")


def _service_key() -> str:
    return str(getattr(get_settings(), "log_report_key", "") or "").strip()


def _endpoint() -> str:
    """由 log_report_endpoint（.../prod-api/standalone-log）推导 standalone-auth。"""
    ep = str(getattr(get_settings(), "log_report_endpoint", "") or "").strip()
    if not ep:
        return ""
    if ep.endswith("/prod-api/standalone-log"):
        return ep[: -len("/prod-api/standalone-log")] + "/prod-api/standalone-auth"
    if "/prod-api/" in ep:
        return ep.rsplit("/prod-api/", 1)[0] + "/prod-api/standalone-auth"
    return ""


async def verify_remote_credentials(email: str, password: str) -> dict | None:
    """用 156 账号校验密码；成功返回账号权益字典，失败/未配置返回 None。"""
    ep, key = _endpoint(), _service_key()
    email = (email or "").strip().lower()
    if not ep or not key or not email or not password:
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                ep,
                headers={"Authorization": f"Bearer {key}"},
                json={"email": email, "password": password},
            )
            r.raise_for_status()
            data = r.json()
    except Exception as exc:  # noqa: BLE001
        logger.info("remote auth unavailable: %s", exc)
        return None
    if not data.get("ok"):
        logger.info("remote auth rejected: %s (%s)", email, data.get("reason"))
        return None
    return data


def entitlement_payload(data: dict) -> dict:
    """把 standalone-auth 返回整形为 user_entitlements 需要的字段。"""
    exp_raw = data.get("subscription_expires_at")
    exp_dt = None
    if exp_raw:
        try:
            exp_dt = datetime.fromisoformat(str(exp_raw).replace("Z", "+00:00"))
        except ValueError:
            exp_dt = None
    return {
        "email": (data.get("email") or "").strip().lower(),
        "plan": str(data.get("plan") or "free")[:32],
        "has_subscription": bool(data.get("has_active_subscription")),
        "subscription_expires_at": exp_dt,
        "remote_balance_fen": int(data.get("balance_fen") or 0),
        "raw": data,
        "synced_at": datetime.now(timezone.utc),
        "last_sync_error": "",
    }
