# -*- coding: utf-8 -*-
"""单机版 → dj.aiwyq.bond 8789 账号校验（第二通道，2026-10-18）。

dj 8789（cloud_server.py + agnes_vip.py）暴露 /m/api/login {email,password}→{ok,token}。
本机登录时，本地与 156（remote_auth）都未命中，回 dj 8789 校验：
命中即 dj/网页端账号，本机自动建号（保留权益，走 user_entitlements 快照）。
失败/未配置静默返回 None，绝不阻塞登录主流程。
"""
from __future__ import annotations

import logging

import httpx

from app.config import get_settings

logger = logging.getLogger("app.dj_8789_auth")

_DEFAULT_DJ_BASE = "https://dj.aiwyq.bond"


def _base() -> str:
    s = str(getattr(get_settings(), "dj_auth_base_url", "") or "").strip()
    return (s or _DEFAULT_DJ_BASE).rstrip("/")


async def dj_verify_credentials(email: str, password: str) -> dict | None:
    """dj 8789 账号校验：成功返回 {ok,email,api_key,...}，失败/未配置返回 None。"""
    base = _base()
    if not email or not password:
        return None
    try:
        async with httpx.AsyncClient(timeout=12.0, verify=False) as client:
            r = await client.post(
                f"{base}/m/api/login",
                json={"email": email.strip().lower(), "password": password},
            )
            if r.status_code != 200:
                return None
            data = r.json()
    except Exception as exc:  # noqa: BLE001
        logger.info("dj 8789 auth unavailable: %s", exc)
        return None
    if not isinstance(data, dict) or not data.get("ok"):
        logger.info("dj 8789 rejected: %s", data.get("msg") if isinstance(data, dict) else "?")
        return None
    return data
