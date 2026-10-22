# -*- coding: utf-8 -*-
"""会员订阅：激活/延长/周期续费/降级。

易支付是一次性支付网关，无原生周期代扣。订阅续费统一由本模块的后台循环执行：
- 支付成功（api/billing.py 回调）→ activate_or_extend：激活或延长当期；
- 每个周期到期（expires_at 到达）→ renew：从用户余额扣本期同款价款；
- 余额不足 → 降级 free（user.plan 回落、status=lapsed、lapsed_at 打点，不产生负余额）；
- 用户取消（POST /billing/subscription/cancel）→ 当期权益保留到 expires_at，之后不再续费。

权益规格（perks）随 SKU 定义在 pricing.py：推理免费、生图每日次数、视频每日
优先生成时长额度（分钟），额度用完降级免费版视频生成（需排队）。权益执行
（生图/视频配额）属下游模块，本模块只负责订阅状态机与扣费。
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models import Subscription, User
from app.services.billing.pricing import sku_by_id

logger = logging.getLogger(__name__)

# 续费扫描间隔（秒）：日卡场景需小时级精度，取 300s（5 分钟）
RENEW_SCAN_INTERVAL_SEC = 300
# 到期宽限（秒）：扫到即扣，避免刚跨边界就因扫描延迟误降级
RENEW_GRACE_SEC = 0
# 停机/欠费积压时单次扫描最多补扣周期数（超出视为放弃，降级 free，防止巨额一次性扣款）
MAX_CATCHUP_PERIODS = 30

_renew_task: asyncio.Task | None = None
_renew_stop = asyncio.Event()
_renew_intentionally_stopped: bool = True


# ===== 状态机 =====


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


async def get_active_subscription(db: AsyncSession, user_id: int) -> Subscription | None:
    """当前活跃（active 且未到期 或 到期宽限内）的订阅。"""
    result = await db.execute(
        select(Subscription)
        .where(Subscription.user_id == int(user_id), Subscription.status == "active")
        .order_by(Subscription.expires_at.desc())
        .limit(1)
    )
    row = result.scalars().all()
    return row[0] if row else None



async def require_accel(db: AsyncSession, user: "User") -> dict:
    """开通/使用 DePIN 加速功能的鉴权闸门（v15 新增）。

    判定优先级：
    1. 活跃订阅且 perks 含 depin_enabled=True（plus/pro 均含 accel_gb_per_day 额度）
    2. 兜底：余额 > 0（按量付费，MVP 允许）

    允许时返回 {"allowed", "reason", "plan", "accel_gb_per_day", "daily_quota_mb"}；
    不允许抛 HTTPException(403)。
    """
    from fastapi import HTTPException

    sub = await get_active_subscription(db, user.id)
    perks = dict(sub.perks or {}) if sub else {}
    depin = bool(perks.get("depin_enabled"))
    accel_gb = int(perks.get("accel_gb_per_day") or 0)
    if sub and depin:
        return {
            "allowed": True,
            "reason": f"subscription:{sub.plan}",
            "plan": sub.plan,
            "accel_gb_per_day": accel_gb,
            "daily_quota_mb": accel_gb * 1024,
        }
    balance = int(getattr(user, "balance_fen", 0) or 0)
    if balance > 0:
        return {
            "allowed": True,
            "reason": f"balance:{balance}fen",
            "plan": "free+payg",
            "accel_gb_per_day": 0,
            "daily_quota_mb": 0,
        }
    # 单向激励模型（v15.x）：普通 free 用户也放行——反代流量对用户免费，
    # 节点侧赚贡献奖励。无配额限制（quota_mb=0），超额 403 仅由 contribute
    # 的 is_sub 分支对订阅用户触发。
    return {
        "allowed": True,
        "reason": "free-oneway",
        "plan": "free",
        "accel_gb_per_day": 0,
        "daily_quota_mb": 0,
    }

async def get_subscription_status(db: AsyncSession, user_id: int) -> dict | None:
    """前台展示：当前/最近一条订阅（含权益快照与到期时间）。"""
    result = await db.execute(
        select(Subscription)
        .where(Subscription.user_id == int(user_id))
        .order_by(Subscription.created_at.desc())
        .limit(1)
    )
    row = result.scalars().first()
    if row is None:
        return None
    return {
        "id": row.id,
        "sku_id": row.sku_id,
        "plan": row.plan,
        "status": row.status,
        "amount_fen": int(row.amount_fen or 0),
        "period_days": int(row.period_days or 0),
        "perks": dict(row.perks or {}),
        "expires_at": _as_utc(row.expires_at).isoformat() if row.expires_at else None,
        "last_renewed_at": _as_utc(row.last_renewed_at).isoformat() if row.last_renewed_at else None,
        "lapsed_at": _as_utc(row.lapsed_at).isoformat() if row.lapsed_at else None,
        "cancelled_at": _as_utc(row.cancelled_at).isoformat() if row.cancelled_at else None,
        "auto_renew": bool(row.auto_renew),
    }


async def activate_or_extend(
    db: AsyncSession,
    user: User,
    sku_id: str,
    *,
    order_no: str = "",
) -> Subscription:
    """支付成功入口：激活新订阅或在现有同档位订阅上延长。

    - 无 active 订阅：新建（expires_at = now + period_days）；
    - 已有 active 订阅（同档位续充或跨档位升级）：从 max(now, expires_at) 延长；
      跨档位升级时切换 sku/plan/perks，按新 SKU 的周期扣费。
    - 同步 user.plan（订阅生效即 pro/plus；free 由 lapsed 回落）。
    """
    sku = sku_by_id(sku_id)
    if sku is None:
        raise ValueError("未知订阅 SKU")
    if str(sku.get("kind") or "") != "subscription":
        raise ValueError("非订阅型 SKU")

    now = _now()
    existing = await get_active_subscription(db, int(user.id))
    if existing is None:
        sub = Subscription(
            user_id=int(user.id),
            sku_id=sku_id,
            plan=str(sku["plan"]),
            status="active",
            amount_fen=int(sku["amount_fen"]),
            period_days=int(sku["period_days"]),
            perks=dict(sku.get("perks") or {}),
            activated_from_order=order_no or None,
            expires_at=now + timedelta(days=int(sku["period_days"])),
            last_renewed_at=now,
        )
        db.add(sub)
    else:
        base = max(_as_utc(existing.expires_at) or now, now)
        existing.sku_id = sku_id
        existing.plan = str(sku["plan"])
        existing.status = "active"
        existing.amount_fen = int(sku["amount_fen"])
        existing.period_days = int(sku["period_days"])
        existing.perks = dict(sku.get("perks") or {})
        existing.expires_at = base + timedelta(days=int(sku["period_days"]))
        existing.last_renewed_at = now
        existing.lapsed_at = None
        existing.cancelled_at = None
        sub = existing

    user.plan = sub.plan
    await db.flush()
    # 审计流水：订阅激活/延长（delta=0 记账，不动钱包；首付款本身由订单入账）
    logger.info(
        "subscription activated user=%s sku=%s plan=%s expires_at=%s order=%s",
        user.id,
        sku_id,
        sub.plan,
        sub.expires_at.isoformat(),
        order_no or "-",
    )
    return sub


async def renew_subscription(db: AsyncSession, sub: Subscription, *, user: User | None = None) -> dict:
    """到期续费：余额足够 → 扣款并延长；不足 → 降级 free。

    多期补扣：距上次支付已跨 N 个完整周期时（如停机/欠费积压），一次性补扣 N 期，
    单行上限 MAX_CATCHUP_PERIODS，超出部分视为放弃降级（防止长期欠费后一次性巨额扣款）。
    幂等：扣款后 expires_at 向后推移，同一次扫描不会重复触发；
    扣款前按用户行锁串行，并发扫描不会重复扣费。
    返回 {"outcome": "renewed" | "lapsed", "charged_fen": int, "periods": int}
    """
    now = _now()
    sku = sku_by_id(sub.sku_id)
    amount = int(sku["amount_fen"]) if sku else int(sub.amount_fen or 0)
    if amount <= 0:
        # 价款异常（SKU 被改）：不扣费直接降级，避免免费白嫖
        sub.status = "lapsed"
        sub.lapsed_at = now
        return {"outcome": "lapsed", "charged_fen": 0, "periods": 0}

    from app.services.billing.settlement import _ledger, _lock_user

    locked = await _lock_user(db, int(sub.user_id))
    if locked is None:
        raise ValueError("用户不存在")

    period_days = max(1, int(sub.period_days or 30))
    # 以原到期时刻为基准（不钳制到 now，否则停机/欠费积压时补扣期数恒为 1）
    base = _as_utc(sub.expires_at) or now
    elapsed = (now - base).total_seconds()
    # 已跨过的完整周期数 + 1（当期）
    periods = max(1, int(elapsed // (period_days * 86400)) + 1)
    periods = min(periods, MAX_CATCHUP_PERIODS)
    total = amount * periods

    available = int(locked.balance_fen or 0)
    if available >= total:
        await _ledger(
            db,
            locked,
            -total,
            "settle",
            ref_type="subscription",
            ref_id=str(sub.id),
            note=f"renew:{sub.sku_id} x{periods}",
        )
        sub.expires_at = base + timedelta(days=period_days * periods)
        sub.last_renewed_at = now
        await db.flush()
        if user is not None:
            user.balance_fen = locked.balance_fen
            user.frozen_fen = locked.frozen_fen
        return {"outcome": "renewed", "charged_fen": total, "periods": periods}

    # 余额不足：降级 free，不产生负余额，无宽限期
    sub.status = "lapsed"
    sub.lapsed_at = now
    locked.plan = "free"
    await db.flush()
    if user is not None:
        user.balance_fen = locked.balance_fen
        user.frozen_fen = locked.frozen_fen
    logger.warning(
        "subscription lapsed user=%s sub=%s sku=%s need_fen=%s (%sx%ss) available_fen=%s",
        sub.user_id,
        sub.id,
        sub.sku_id,
        total,
        periods,
        amount,
        available,
    )
    return {"outcome": "lapsed", "charged_fen": 0, "periods": 0}


async def cancel_subscription(db: AsyncSession, user: User) -> dict:
    """用户手动取消：当期权益保留到 expires_at，之后到期直接 lapsed（不扣费）。"""
    sub = await get_active_subscription(db, int(user.id))
    if sub is None:
        user.plan = "free"
        return {"cancelled": False, "reason": "no active subscription"}
    sub.auto_renew = False
    sub.cancelled_at = _now()
    # 到期前仍 active（权益保留到到期），到期扫描时 auto_renew=False → lapsed
    return {"cancelled": True, "expires_at": _as_utc(sub.expires_at).isoformat()}


async def process_due_renewals(db: AsyncSession, *, limit: int = 100) -> int:
    """扫描到期订阅：auto_renew=True 尝试续费，否则直接 lapsed。

    由后台循环周期调用；返回处理条数。逐用户独立事务，单行失败不影响其他。
    """
    now = _now()
    stmt = (
        select(Subscription)
        .where(
            Subscription.status == "active",
            Subscription.expires_at <= now + timedelta(seconds=RENEW_GRACE_SEC),
        )
        .limit(limit)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    if not rows:
        return 0
    processed = 0
    for sub in rows:
        try:
            user = await db.get(User, int(sub.user_id))
            if user is None:
                continue
            if sub.auto_renew:
                result = await renew_subscription(db, sub, user=user)
            else:
                sub.status = "lapsed"
                sub.lapsed_at = now
                user.plan = "free"
                result = {"outcome": "cancelled-lapse", "charged_fen": 0}
            await db.commit()
            processed += 1
            logger.info(
                "subscription renewal due sub=%s user=%s outcome=%s charged_fen=%s",
                sub.id,
                sub.user_id,
                result["outcome"],
                result["charged_fen"],
            )
        except Exception:  # noqa: BLE001
            logger.exception("subscription renewal failed sub=%s user=%s", sub.id, sub.user_id)
            await db.rollback()
    return processed


# ===== 后台循环 =====


async def _renewal_loop() -> None:
    # 订阅续费/降级是用户付费承诺，不受全局按量计费开关（billing_enabled）影响：
    # 已支付的订阅必须履约，余额不足则降级 free，否则会出现付费后权益不生效。
    while not _renew_stop.is_set():
        try:
            async with AsyncSessionLocal() as db:
                await process_due_renewals(db)
        except Exception:  # noqa: BLE001
            logger.exception("subscription renewal loop tick failed")
        try:
            await asyncio.wait_for(_renew_stop.wait(), timeout=RENEW_SCAN_INTERVAL_SEC)
        except asyncio.TimeoutError:
            pass


async def start_subscription_renewer() -> None:
    global _renew_task, _renew_intentionally_stopped
    if _renew_task and not _renew_task.done():
        return
    _renew_intentionally_stopped = False
    _renew_stop.clear()
    _renew_task = asyncio.create_task(_renewal_loop(), name="subscription-renewer")


async def stop_subscription_renewer() -> None:
    global _renew_intentionally_stopped
    _renew_intentionally_stopped = True
    _renew_stop.set()
    if _renew_task and not _renew_task.done():
        _renew_task.cancel()
        try:
            await _renew_task
        except asyncio.CancelledError:
            pass


def subscription_renewer_status() -> str:
    if _renew_intentionally_stopped:
        return "stopped"
    if _renew_task and not _renew_task.done():
        return "running"
    return "stopped"


# ===== 权益今日用量统计 =====


async def get_perks_usage(db: AsyncSession, user_id: int) -> dict:
    """今日（北京时间）权益用量：生图调用次数 + 视频优先生成分钟数。

    用于订阅页展示「今日已用 X / 额度」。生图按 usage_events 行数计；
    视频时长优先取用量行 raw_usage_json 里的 duration_sec，缺失时按估算
    token ÷ 每时长 token 还原（与写入侧估算口径一致）。
    """
    from zoneinfo import ZoneInfo

    from app.models import UsageEvent

    now_cn = datetime.now(ZoneInfo("Asia/Shanghai"))
    day_start_cn = now_cn.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start_utc = day_start_cn.astimezone(UTC)

    rows = (
        (
            await db.execute(
                select(UsageEvent.billing_key, UsageEvent.total_tokens, UsageEvent.raw_usage_json)
                .where(
                    UsageEvent.user_id == int(user_id),
                    UsageEvent.capability.in_(["image", "video"]),
                    UsageEvent.created_at >= day_start_utc,
                )
            )
        ).all()
    )
    image_calls = 0
    video_seconds = 0.0
    for key, tokens, raw_json in rows:
        if key == "seedream":
            image_calls += 1
            continue
        if key.startswith("seedance"):
            sec = None
            if raw_json:
                try:
                    raw = json.loads(raw_json)
                    if isinstance(raw, dict):
                        usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else raw
                        for src in (usage, raw):
                            if isinstance(src, dict):
                                if src.get("duration_sec"):
                                    sec = float(src["duration_sec"])
                                    break
                except (ValueError, TypeError):
                    pass
            if sec is None:
                from app.config import get_settings

                per_sec = max(float(get_settings().billing_est_seedance_tokens_per_sec), 1.0)
                sec = float(int(tokens or 0)) / per_sec
            video_seconds += sec
    return {
        "day_start_cn": day_start_cn.strftime("%Y-%m-%d"),
        "image_calls": image_calls,
        "video_seconds": int(round(video_seconds)),
    }
