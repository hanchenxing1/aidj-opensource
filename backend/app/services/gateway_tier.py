# -*- coding: utf-8 -*-
"""网关档位：按订阅在 new-api 双 token 间切换（vip 池 / 免费池）。

- 提交时定档：任务创建/请求入口按用户活跃订阅打 payload["gateway_tier"] 标签（零 DB 迁移）；
  执行时（executor/poller/同步 ephemeral）按标签设 contextvar。
- key 注入点（ArkGateway._ark_api_key / chat_completions / resolve_tokenfree_api_key）
  在档位=vip 且 vip key 存在时覆盖为 vip key；否则保持现状（free/default 池）。
- vip key 存于 system_model_channels 行 tokenfree_vip（enabled=0 仅存 key，
  由 _ensure_tokenfree_channel 维持禁用，不参与逻辑模型路由；key 快照按全量行
  读取，故 enabled 过滤不影响取 key）。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User

logger = logging.getLogger("app.gateway_tier")

TIERS = ("free", "vip")
CONTEXT_TIER: ContextVar[str] = ContextVar("gateway_tier", default="free")
PAYLOAD_TIER_KEY = "gateway_tier"
VIP_CHANNEL_ID = "tokenfree_vip"


# 临时切换网关档位（任务/请求执行入口使用）。
@asynccontextmanager
async def gateway_tier_scope(tier: str) -> AsyncIterator[None]:
    t = str(tier or "free").strip().lower()
    t = t if t in TIERS else "free"
    token = CONTEXT_TIER.set(t)
    try:
        yield
    finally:
        CONTEXT_TIER.reset(token)


# 读任务 payload 里的档位标签。
def payload_tier(payload) -> str:
    if isinstance(payload, dict):
        return str(payload.get(PAYLOAD_TIER_KEY) or "free")
    return "free"


# 提交时定档：活跃订阅 -> vip，否则 free（订阅查询失败降级 free，不阻断任务）。
async def tier_for_user(db: AsyncSession, user: User | None) -> str:
    if user is None:
        return "free"
    try:
        from app.services.billing.subscription import get_active_subscription

        sub = await get_active_subscription(db, user.id)
        if sub is not None:
            return "vip"
    except Exception:  # noqa: BLE001
        logger.warning("gateway_tier: subscription lookup failed user=%s, fallback free", user.id)
    return "free"


# 把档位标签写进任务 payload（提交时定档，任务中途订阅变化不影响已付履约）。
async def stamp_payload_tier(db: AsyncSession, user: User, payload: dict | None) -> dict:
    data = dict(payload or {})
    data[PAYLOAD_TIER_KEY] = await tier_for_user(db, user)
    return data


def _vip_api_key() -> str:
    """vip 池 key（tokenfree_vip 行仅存 key，启用与否不参与路由；按全量快照读取）。"""
    try:
        from app.services.model_settings import get_routing_snapshot

        channels = get_routing_snapshot().channels
    except Exception:  # noqa: BLE001
        return ""
    for ch in channels:
        if ch.id == VIP_CHANNEL_ID and (ch.api_key or "").strip():
            return ch.api_key.strip()
    return ""


def gateway_api_key_override() -> str:
    """当前执行档位为 vip 且 vip key 可用时返回 vip key；否则空串=不覆盖（保持现状 free 行为）。"""
    if CONTEXT_TIER.get() == "vip":
        key = _vip_api_key()
        if not key:
            logger.warning("gateway_tier: vip tier but tokenfree_vip key missing, fallback free")
        return key
    return ""


__all__ = [
    "CONTEXT_TIER",
    "PAYLOAD_TIER_KEY",
    "VIP_CHANNEL_ID",
    "gateway_api_key_override",
    "gateway_tier_scope",
    "payload_tier",
    "stamp_payload_tier",
    "tier_for_user",
]
