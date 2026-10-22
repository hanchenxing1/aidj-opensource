# -*- coding: utf-8 -*-
"""单机版 → 156 权益同步：拉取 156 该账号的订阅/余额状态，落本机 user_entitlements。

设计（2026-10-18）：
- 156 侧 /prod-api/entitlement-sync（服务间 key 鉴权，同 standalone-log 机制）。
- 本机用「绑定的 156 账号 email」查询该账号在 156 侧：plan / active 订阅 / 余额。
- 落本机 user_entitlements 表；绑定/手动同步/访问权益端点时调用 sync。
- 保留权益判定：
  * 156 上有 active 订阅（未到期）或余额>0 或档位非 free → retained=True（不受赠额上限约束）；
  * 纯新用户（156 上无订阅且余额为 0）→ 仅享本机 10 元 7 天注册赠额。
- 离线降级：156 不可达时沿用最近一次同步的权益快照。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models_entitlement import UserEntitlement

logger = logging.getLogger("app.entitlement_sync")


def _service_key() -> str:
    s = get_settings()
    return str(getattr(s, "log_report_key", "") or "").strip()


def _endpoint() -> str:
    s = get_settings()
    ep = str(getattr(s, "log_report_endpoint", "") or "").strip()
    if ep.endswith("/prod-api/standalone-log"):
        return ep[: -len("/prod-api/standalone-log")] + "/prod-api/entitlement-sync"
    if "/prod-api/" in ep:
        return ep.rsplit("/prod-api/", 1)[0] + "/prod-api/entitlement-sync"
    return ""


def bound_email() -> str:
    """本机绑定的 156 账号邮箱（data/bound_email.txt）。"""
    base = Path(__file__).resolve().parent.parent.parent.parent
    bindfile = base / "data" / "bound_email.txt"
    if bindfile.is_file():
        return bindfile.read_text(encoding="utf-8-sig").strip().lower()
    return ""


async def sync_entitlements(email: str | None = None) -> dict:
    """从 156 同步指定账号权益；失败返回 last_error，不影响本机既有状态。"""
    ep = _endpoint()
    key = _service_key()
    email = (email or bound_email() or "").strip().lower()
    if not ep or not key or not email:
        return {"ok": False, "reason": "endpoint/key/email not configured"}

    result: dict = {"ok": False}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                ep,
                headers={"Authorization": f"Bearer {key}"},
                json={"email": email},
            )
            r.raise_for_status()
            data = r.json()

        async with AsyncSessionLocal() as db:
            if data.get("found"):
                exp_raw = data.get("subscription_expires_at")
                exp_dt: datetime | None = None
                if exp_raw:
                    try:
                        exp_dt = datetime.fromisoformat(str(exp_raw))
                    except ValueError:
                        exp_dt = None
                db.add(
                    UserEntitlement(
                        email=email,
                        plan=str(data.get("plan") or "free")[:32],
                        has_subscription=bool(data.get("has_active_subscription")),
                        subscription_expires_at=exp_dt,
                        remote_balance_fen=int(data.get("balance_fen") or 0),
                        last_sync_error="",
                        synced_at=datetime.now(timezone.utc),
                        raw=data,
                    )
                )
            else:
                # 账号不存在：落一条 found 标记，避免误判为"无同步记录"
                row = (
                    await db.execute(select(UserEntitlement).where(UserEntitlement.email == email))
                ).scalar_one_or_none()
                if row is None:
                    db.add(UserEntitlement(email=email, last_sync_error="remote account not found"))
            await db.commit()

        if not data.get("found"):
            result.update(ok=True, found=False, reason="no remote account")
            return result
        result.update(
            ok=True,
            found=True,
            plan=data.get("plan"),
            has_subscription=data.get("has_active_subscription"),
            remote_balance_fen=data.get("balance_fen"),
        )
        logger.info("entitlement sync ok: %s", email)
    except Exception as exc:  # noqa: BLE001
        result.update(ok=False, last_error=str(exc))
        try:
            async with AsyncSessionLocal() as db:
                q = await db.execute(select(UserEntitlement).where(UserEntitlement.email == email))
                ent = q.scalars().all()
                if ent:
                    ent[-1].last_sync_error = str(exc)[:500]
                    await db.commit()
        except Exception:  # noqa: BLE001
            pass
        logger.warning("entitlement sync failed: %s (%s)", email, exc)
    return result


def _latest_entitlement(db: AsyncSession, email: str):
    return (
        select(UserEntitlement)
        .where(UserEntitlement.email == email)
        .order_by(UserEntitlement.synced_at.desc().nullslast(), UserEntitlement.id.desc())
        .limit(1)
    )


def _is_retained_from_row(row: UserEntitlement | None) -> bool:
    """单行 user_entitlements 快照的保留权益判定。"""
    if row is None:
        return False
    if row.has_subscription:
        exp = row.subscription_expires_at
        # 订阅未到期（或无到期时间视为长期）
        if exp is None or exp >= datetime.now(timezone.utc):
            return True
    if int(row.remote_balance_fen or 0) > 0:
        return True
    if (row.plan or "free") not in ("", "free"):
        return True
    return False


async def is_retained_entitlement(db: AsyncSession, user) -> bool:
    """任务入队前判定的保留权益豁免（2026-10-18）。

    历史注册用户满足以下任一即视为"保留权益"，任务入队不预扣/不受赠额门控：
    1. 本机该用户自身 balance_fen > 0 或 plan 非 free（历史本机充值/开通过）；
    2. 本机绑定的 156 账号在 156 侧有 active 订阅 / 余额 / 非 free 档位（user_entitlements 快照）。

    纯新用户（两者都不满足）→ 走 billing_signup_grant_fen 赠额门控。
    离线降级：156 侧状态取 user_entitlements 最近同步快照；从未同步/同步失败=按本机判定。
    """
    if int(getattr(user, "balance_fen", 0) or 0) > 0:
        return True
    if (getattr(user, "plan", None) or "free") != "free":
        return True

    # 优先按「当前登录用户自己的邮箱」查 156 权益快照（多账号/网页版注册用户直接登录场景），
    # 再回落到本机绑定的 156 账号（单机免登录内置账号场景）。
    own = (getattr(user, "email", "") or "").strip().lower()
    if own:
        row = (await db.execute(_latest_entitlement(db, own))).scalar_one_or_none()
        if _is_retained_from_row(row):
            return True
    bound = bound_email()
    if not bound:
        return False
    row = (await db.execute(_latest_entitlement(db, bound))).scalar_one_or_none()
    return _is_retained_from_row(row)


def retention_snapshot(email: str | None = None) -> dict:
    """同步（非异步）权益快照，供 /api/standalone/entitlements 使用。

    读取本机 SQLite（同步连接），不依赖事件循环；返回含 retained / sync_ok。
    """
    import sqlite3

    email = (email or bound_email() or "").strip().lower()
    dbfile = ""
    try:
        from app.config import get_settings as _gs
        url = _gs().database_url or ""
        # sqlite+aiosqlite:///D:/.../data/printfilm.db
        if url.startswith("sqlite+aiosqlite:///"):
            dbfile = url.replace("sqlite+aiosqlite:///", "")
        elif url.startswith("sqlite:///"):
            dbfile = url.replace("sqlite:///", "")
    except Exception:  # noqa: BLE001
        pass
    if not dbfile:
        base = Path(__file__).resolve().parent.parent.parent
        dbfile = str(base / "data" / "printfilm.db")

    ent: dict | None = None
    if email and Path(dbfile).is_file():
        try:
            con = sqlite3.connect(dbfile, timeout=5)
            con.row_factory = sqlite3.Row
            cur = con.execute(
                "SELECT * FROM user_entitlements WHERE email=? ORDER BY COALESCE(synced_at, created_at) DESC, id DESC LIMIT 1",
                (email,),
            )
            row = cur.fetchone()
            con.close()
            if row is not None:
                d = dict(row)
                ent = {
                    "email": d.get("email"),
                    "plan": d.get("plan") or "free",
                    "has_subscription": bool(d.get("has_subscription")),
                    "subscription_expires_at": d.get("subscription_expires_at"),
                    "remote_balance_fen": int(d.get("remote_balance_fen") or 0),
                    "synced_at": d.get("synced_at"),
                    "last_sync_error": d.get("last_sync_error") or "",
                }
        except Exception as exc:  # noqa: BLE001
            logger.debug("retention snapshot read failed: %s", exc)

    # 保留权益判定（复用行级逻辑，用 dict 手动判定）
    retained = False
    if ent:
        has_sub = bool(ent.get("has_subscription"))
        exp = ent.get("subscription_expires_at")
        exp_ok = True
        if exp:
            try:
                exp_dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
                exp_ok = exp_dt >= datetime.now(timezone.utc)
            except ValueError:
                exp_ok = True
        if has_sub and exp_ok:
            retained = True
        if int(ent.get("remote_balance_fen") or 0) > 0:
            retained = True
        if (ent.get("plan") or "free") not in ("", "free"):
            retained = True

    return {
        "email": email,
        "entitlement": ent,
        "retained": retained,
        "sync_ok": bool(ent and not ent.get("last_sync_error")),
    }

