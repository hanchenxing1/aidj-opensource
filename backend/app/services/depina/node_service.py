# -*- coding: utf-8 -*-
"""depina.node_service — 单机版"加速服务"开关 + 向 156 中心注册/心跳/奖励查询。

设计（2026-10-18）：
- 单机版节点（本 App 的 sidecar）在用户家里、NAT 后无法被网页端直连，
  需经 156 中心的服务间端点把 peer_id 登记进 depina_nodes 注册表：
    开启加速：POST /prod-api/standalone-node/register（首次 + 60s 心跳）
    关闭加速：POST /prod-api/standalone-node/deregister（proxy_enabled=False）
    查奖励：  POST /prod-api/standalone-node/contribution（累计贡献/分）
- 本机开关数据面 = sidecar set_proxy_enabled（是否接受中心 P2P 反代请求）；
  156 中心选到本节点后，由中心 sidecar 向本节点发 http_proxy 流，本节点
  反代源站并把响应回流。贡献奖励由 156 中心自动入账节点 owner（1分/KB≈1秒视频）。
- 鉴权：156 服务间端点用 LOG_REPORT_KEY（与 standalone-auth 同一服务 key）+
  本机绑定的 156 账号邮箱（data/bound_email.txt；未绑定回退内置 STANDALONE_EMAIL）。

线程/任务安全：
- 心跳用 asyncio.Task，开关即启停；重复开启幂等（已有 task 直接返回）。
- 心跳失败静默（156 不可达不影响本机；恢复后下次心跳自动重新登记）。
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

import httpx

from app.config import get_settings
from app.services.depina.sidecar_client import SidecarError

logger = logging.getLogger("app.depina.node_service")

# 心跳间隔（秒）：156 侧 STALE_AFTER_SEC=90，取 60s 保证注册表内 90s 内必有心跳
HEARTBEAT_INTERVAL_SEC = 60

_heartbeat_task: Optional[asyncio.Task] = None


def _service_key() -> str:
    return str(getattr(get_settings(), "log_report_key", "") or "").strip()


def _node_base_url() -> str:
    """由 log_report_endpoint（.../prod-api/standalone-log）推导 standalone-node 端点。"""
    ep = str(getattr(get_settings(), "log_report_endpoint", "") or "").strip()
    if not ep:
        return ""
    if "/prod-api/" in ep:
        return ep.rsplit("/prod-api/", 1)[0]
    return ""


def _owner_email() -> str:
    """本机绑定的 156 账号邮箱（156 侧节点 owner）；未绑定回退内置 standalone 邮箱。"""
    from app.services.billing.entitlement_sync import bound_email

    bound = bound_email()
    if bound:
        return bound
    # 未绑定：用本机内置 standalone 邮箱（156 侧若存在该账号则绑定，否则 404 静默）
    return os.environ.get("STANDALONE_ACCEL_OWNER_EMAIL", "standalone@example.com")


async def node_status() -> dict:
    """本机加速服务当前状态（sidecar 开关 + 心跳 task 是否活跃）。"""
    from app.api import depina as depina_api

    mgr = depina_api._get_mgr()
    sidecar_running = mgr.is_running()
    proxy_enabled = False
    peer_id = ""
    listen_addrs: list[str] = []
    if sidecar_running:
        try:
            st = await mgr.status()
            peer_id = st.get("peerId") or ""
            listen_addrs = st.get("listenAddrs") or []
            proxy_enabled = bool(st.get("proxyEnabled", False))
        except SidecarError as e:
            return {
                "sidecar_running": sidecar_running,
                "proxy_enabled": False,
                "peer_id": "",
                "listen_addrs": [],
                "heartbeat_active": bool(_heartbeat_task and not _heartbeat_task.done()),
                "error": f"sidecar status: {e}",
            }
    return {
        "sidecar_running": sidecar_running,
        "proxy_enabled": proxy_enabled,
        "peer_id": peer_id,
        "listen_addrs": listen_addrs,
        "heartbeat_active": bool(_heartbeat_task and not _heartbeat_task.done()),
    }


def _http_headers() -> dict:
    return {"Authorization": f"Bearer {_service_key()}"}


async def _register_once(peer_id: str, listen_addrs: list[str], owner_email: str) -> dict:
    """向 156 中心登记一次（注册/心跳）。返回 156 响应 dict；失败返回 {"ok": False, ...}。"""
    base = _node_base_url()
    key = _service_key()
    if not base or not key:
        return {"ok": False, "reason": "log_report_endpoint/key 未配置，无法向 156 登记"}
    url = base + "/prod-api/standalone-node/register"
    payload = {
        "email": owner_email,
        "peer_id": peer_id,
        "ip": "",
        "listen_addrs": listen_addrs or [],
        "proxy_enabled": True,
        "allowed_hosts": [],
    }
    try:
        async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
            r = await client.post(url, json=payload, headers=_http_headers())
            data = r.json()
        if r.status_code != 200:
            return {"ok": False, "status": r.status_code, "detail": data}
        return {"ok": True, "node": data.get("node")}
    except Exception as e:  # noqa: BLE001 — 156 不可达静默降级
        return {"ok": False, "reason": f"156 登记失败: {type(e).__name__}: {e}"}


async def _heartbeat_loop(peer_id: str, listen_addrs: list[str], owner_email: str) -> None:
    global _heartbeat_task
    logger.info("accel heartbeat started peer=%s owner=%s interval=%ds", peer_id, owner_email, HEARTBEAT_INTERVAL_SEC)
    # 立即登记一次
    res = await _register_once(peer_id, listen_addrs, owner_email)
    if res.get("ok"):
        logger.info("accel registered to 156 ok")
    else:
        logger.warning("accel register failed: %s", res.get("reason") or res.get("detail"))
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)
        if _heartbeat_task is not asyncio.current_task():
            break
        res = await _register_once(peer_id, listen_addrs, owner_email)
        if res.get("ok"):
            logger.debug("accel heartbeat ok")
        else:
            logger.info("accel heartbeat miss: %s", res.get("reason") or res.get("detail"))


async def start_heartbeat() -> dict:
    """开启加速服务：sidecar 开反代 + 向 156 注册 + 60s 心跳。幂等。"""
    global _heartbeat_task
    from app.api import depina as depina_api

    if _heartbeat_task and not _heartbeat_task.done():
        return {"ok": True, "already": True, "note": "心跳已在运行"}

    mgr = depina_api._get_mgr()
    if not mgr.is_running():
        try:
            await mgr.start()
        except SidecarError as e:
            return {"ok": False, "reason": f"sidecar 启动失败: {e}"}
    st = await mgr.status()
    peer_id = st.get("peerId") or ""
    listen_addrs = st.get("listenAddrs") or []
    if not peer_id:
        return {"ok": False, "reason": "sidecar 无 peer_id，无法登记 156"}

    try:
        await mgr.set_proxy_enabled(True)
    except SidecarError as e:
        return {"ok": False, "reason": f"开反代失败: {e}"}

    owner_email = _owner_email()
    _heartbeat_task = asyncio.create_task(
        _heartbeat_loop(peer_id, listen_addrs, owner_email), name="depina-node-heartbeat"
    )
    return {"ok": True, "peer_id": peer_id, "owner_email": owner_email, "interval_sec": HEARTBEAT_INTERVAL_SEC}


async def stop_heartbeat() -> dict:
    """关闭加速服务：sidecar 关反代 + 注销 156 + 停心跳。幂等。"""
    global _heartbeat_task
    from app.api import depina as depina_api

    mgr = depina_api._get_mgr()
    if mgr.is_running():
        try:
            st = await mgr.status()
            await mgr.set_proxy_enabled(False)
            # 向 156 注销（失败静默，节点 90s 无心跳自然离线）
            peer_id = st.get("peerId") or ""
            base = _node_base_url()
            if peer_id and base:
                url = base + "/prod-api/standalone-node/deregister"
                try:
                    async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
                        await client.post(
                            url,
                            json={"email": _owner_email(), "peer_id": peer_id},
                            headers=_http_headers(),
                        )
                except Exception as e:  # noqa: BLE001
                    logger.info("accel deregister miss: %s", e)
        except SidecarError as e:
            logger.warning("accel stop sidecar 关反代失败: %s", e)

    task = _heartbeat_task
    _heartbeat_task = None
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    return {"ok": True, "note": "加速服务已关闭，156 注册表内 90s 后自然离线"}


async def reward_snapshot(peer_id: str = "") -> dict:
    """查询本机节点在 156 中心的累计贡献/奖励（分）与折算视频时长（秒）。"""
    from app.api import depina as depina_api

    mgr = depina_api._get_mgr()
    if not peer_id:
        if mgr.is_running():
            try:
                peer_id = (await mgr.status()).get("peerId") or ""
            except SidecarError:
                peer_id = ""
    base = _node_base_url()
    key = _service_key()
    if not base or not key or not peer_id:
        return {"ok": False, "reason": "本机 sidecar 未运行或 156 端点未配置"}
    url = base + "/prod-api/standalone-node/contribution"
    try:
        async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
            r = await client.post(
                url,
                json={"email": _owner_email(), "peer_id": peer_id},
                headers=_http_headers(),
            )
            data = r.json()
        if r.status_code != 200:
            return {"ok": False, "status": r.status_code, "detail": data}
        return data
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"156 奖励查询失败: {type(e).__name__}: {e}"}
