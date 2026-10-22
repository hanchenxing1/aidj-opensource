# -*- coding: utf-8 -*-
"""depina.registry — 中心节点注册表 + 就近选路（MVP）。

设计：
- 客户端节点（装了 App、开通加速功能的用户机器）启动时向中心 156 注册：
  POST /api/depina/registry/register  写入 depina_nodes 表
- 网页端用户请求某 URL 时，先调 156：
  GET  /api/depina/registry/nearby?url=...
  中心从注册表里挑一个"已开通 + 可服务该 host + 最近活跃"的节点，
  返回该节点给网页端；网页端再直连该节点 sidecar 的 http_proxy。
- 节点定期心跳（register 重复调用即可），last_seen_at 刷新。

就近策略（MVP，v1）：
  1. proxy_enabled == True
  2. allowed_hosts 包含目标 host（或节点未设白名单 = 全放行）
  3. last_seen_at 距今 < STALE_AFTER_SEC（默认 90s）
  4. 按 served_bytes 升序取（新贡献少 = 轻负载），或随机；MVP 随机
  5. 无可用节点 → 返回 {"node": null}，网页端回退直连源站
"""
from __future__ import annotations

import random
import re
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models_depina import DepinaAccelUsage, DepinaNode

# 注册超过该秒数没心跳视为离线
STALE_AFTER_SEC = 90

# 贡献奖励：每 KB 反代流量奖励的分值（DePIN 节点赚加速费；MVP 可配）
# 1 分/KB = 1 元/GB。节点每代理 1GB 流量赚 ¥1（free trial 量级，生产可调高）
ACCEL_REWARD_FEN_PER_KB = 1

# payg 超量扣费率：每 KB 反代流量扣的分值（订阅用户超额后按此扣余额）
ACCEL_PAYG_FEN_PER_KB = 1


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def get_daily_usage(user_id: int) -> dict:
    """取用户当日（UTC）加速流量用量。无记录返回 0 值。"""
    today = _now_utc().date()
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(DepinaAccelUsage).where(
                    DepinaAccelUsage.user_id == int(user_id),
                    DepinaAccelUsage.day == today,
                )
            )
        ).scalar_one_or_none()
    if row is None:
        return {"user_id": int(user_id), "day": today.isoformat(), "served_bytes": 0,
                "payg_bytes": 0, "payg_fen": 0}
    return {
        "user_id": int(user_id), "day": today.isoformat(),
        "served_bytes": int(row.served_bytes or 0),
        "payg_bytes": int(row.payg_bytes or 0),
        "payg_fen": int(row.payg_fen or 0),
    }


async def record_daily_usage(
    user_id: int,
    served_bytes: int,
    payg_bytes: int = 0,
    payg_fen: int = 0,
    session=None,
) -> dict:
    """累加用户当日加速流量用量（幂等 upsert，按 user+day 唯一）。

    session: 可传入（与调用方同事务）；不传则自开 AsyncSessionLocal。
    """
    today = _now_utc().date()

    async def _do(sess, commit: bool) -> dict:
        row = (
            await sess.execute(
                select(DepinaAccelUsage).where(
                    DepinaAccelUsage.user_id == int(user_id),
                    DepinaAccelUsage.day == today,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = DepinaAccelUsage(
                user_id=int(user_id), day=today,
                served_bytes=0, payg_bytes=0, payg_fen=0,
            )
            sess.add(row)
        row.served_bytes = int(row.served_bytes or 0) + int(served_bytes)
        row.payg_bytes = int(row.payg_bytes or 0) + int(payg_bytes)
        row.payg_fen = int(row.payg_fen or 0) + int(payg_fen)
        await sess.flush()
        if commit:
            await sess.commit()
            await sess.refresh(row)
        return {
            "user_id": int(user_id), "day": today.isoformat(),
            "served_bytes": row.served_bytes, "payg_bytes": row.payg_bytes,
            "payg_fen": row.payg_fen,
        }

    if session is not None:
        return await _do(session, commit=False)
    async with AsyncSessionLocal() as own:
        return await _do(own, commit=True)


async def register_node(
    *,
    peer_id: str,
    ip: str = "",
    listen_addrs: list[str] | str = "",
    proxy_enabled: bool = False,
    allowed_hosts: list[str] | str = "",
    user_id: int | None = None,
) -> DepinaNode:
    """客户端节点注册/心跳。已存在则更新字段，刷新 last_seen_at。"""
    if isinstance(listen_addrs, (list, tuple)):
        listen_addrs = ",".join(listen_addrs)
    if isinstance(allowed_hosts, (list, tuple)):
        allowed_hosts = ",".join(allowed_hosts)

    async with AsyncSessionLocal() as session:
        from sqlalchemy import select

        row = (
            (await session.execute(select(DepinaNode).where(DepinaNode.peer_id == peer_id)))
            .scalar_one_or_none()
        )
        if row is None:
            row = DepinaNode(peer_id=peer_id)
            session.add(row)
        row.ip = ip
        row.listen_addrs = listen_addrs
        row.proxy_enabled = proxy_enabled
        row.allowed_hosts = allowed_hosts
        if user_id is not None:
            row.user_id = user_id
        row.last_seen_at = _now_utc()
        await session.commit()
        await session.refresh(row)
        return row


async def find_nearby_node(
    url: str,
    limit: int = 1,
) -> DepinaNode | None:
    """按 url 挑一个可用加速节点。返回 None 表示回退直连源站。"""
    host = _extract_host(url)
    async with AsyncSessionLocal() as session:
        from sqlalchemy import select

        candidates = (
            (
                await session.execute(
                    select(DepinaNode).where(DepinaNode.proxy_enabled.is_(True))
                )
            )
            .scalars()
            .all()
        )
        cutoff = _now_utc() - timedelta(seconds=STALE_AFTER_SEC)
        ok: list[DepinaNode] = []
        for n in candidates:
            if n.last_seen_at and n.last_seen_at < cutoff:
                continue  # 离线
            hosts = {h.strip() for h in (n.allowed_hosts or "").split(",") if h.strip()}
            if hosts and host and host not in hosts:
                continue  # 白名单不含目标 host
            ok.append(n)
        if not ok:
            return None
        # MVP：轻负载优先（served_bytes 少）+ 随机打破平局
        ok.sort(key=lambda n: n.served_bytes or 0)
        pick = ok[: max(1, limit)]
        return random.choice(pick)


async def record_contribution(
    peer_id: str,
    served_bytes: int,
    peer_count: int = 1,
    session=None,
) -> dict:
    """加速数据面成功后，节点贡献 +1 并按流量计费。

    返回 {"served_bytes", "reward_fen", "total_reward_fen"} 供 API 层入账钱包。
    奖励规则：每 KB 反代流量奖励 ACCEL_REWARD_FEN_PER_KB 分（1 分/KB = 1 元/GB，MVP 可配）。

    session 可传入（与调用方同事务，保证节点 reward_fen 与用户 balance_fen 原子提交）；
    不传则自开 AsyncSessionLocal（独立事务）。
    """
    from sqlalchemy import select

    kb_served = max(1, (served_bytes + 1023) // 1024)  # 向上取整 KB
    reward_fen = kb_served * ACCEL_REWARD_FEN_PER_KB

    async def _do(sess, commit: bool) -> dict:
        row = (
            (await sess.execute(select(DepinaNode).where(DepinaNode.peer_id == peer_id)))
            .scalar_one_or_none()
        )
        if row is None:
            return {"served_bytes": served_bytes, "reward_fen": reward_fen, "total_reward_fen": 0}
        row.served_bytes = (row.served_bytes or 0) + served_bytes
        row.served_peers = (row.served_peers or 0) + peer_count
        row.reward_fen = (row.reward_fen or 0) + reward_fen
        row.last_seen_at = _now_utc()
        await sess.flush()
        if commit:
            await sess.commit()
        return {
            "served_bytes": served_bytes,
            "reward_fen": reward_fen,
            "total_reward_fen": row.reward_fen,
        }

    if session is not None:
        return await _do(session, commit=False)
    async with AsyncSessionLocal() as own:
        return await _do(own, commit=True)


def _extract_host(url: str) -> str:
    m = re.match(r"^https?://([^/:]+)", url.strip())
    return m.group(1).lower() if m else ""


def node_to_dict(n: DepinaNode) -> dict:
    return {
        "peer_id": n.peer_id,
        "ip": n.ip,
        "listen_addrs": n.listen_addrs,
        "proxy_enabled": n.proxy_enabled,
        "allowed_hosts": n.allowed_hosts,
        "registered_at": n.registered_at.isoformat() if n.registered_at else None,
        "last_seen_at": n.last_seen_at.isoformat() if n.last_seen_at else None,
        "served_bytes": n.served_bytes,
        "served_peers": n.served_peers,
    }


def registry_diagnose() -> dict:
    """排障用：打印注册表全部节点 + 过滤逻辑逐项命中情况（同步，供脚本调用）。"""
    import datetime as _dt

    now = _dt.datetime.now(_dt.timezone.utc)
    cutoff = now - _dt.timedelta(seconds=STALE_AFTER_SEC)
    # 用同步引擎读，避免在脚本里 asyncio.run
    from app.database import engine
    from sqlalchemy import select

    def _run():
        import asyncio

        async def _q():
            async with AsyncSessionLocal() as session:
                return (await session.execute(select(DepinaNode))).scalars().all()

        try:
            return asyncio.run(_q())
        except RuntimeError:
            # 已在事件循环内（不该发生），退化为新建 loop
            import asyncio as _a

            loop = _a.new_event_loop()
            try:
                return loop.run_until_complete(_q())
            finally:
                loop.close()

    rows = _run()
    out = []
    for n in rows:
        hosts = {h.strip() for h in (n.allowed_hosts or "").split(",") if h.strip()}
        out.append(
            {
                "peer_id": n.peer_id,
                "proxy_enabled": n.proxy_enabled,
                "allowed_hosts": n.allowed_hosts,
                "last_seen_at": n.last_seen_at.isoformat() if n.last_seen_at else None,
                "stale": bool(n.last_seen_at and n.last_seen_at < cutoff),
                "would_pass_activity": not (n.last_seen_at and n.last_seen_at < cutoff),
                "hosts_empty": not hosts,
                "served_bytes": n.served_bytes,
            }
        )
    return {"count": len(rows), "stale_after_sec": STALE_AFTER_SEC, "nodes": out}
