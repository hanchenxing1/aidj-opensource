# -*- coding: utf-8 -*-
"""depina — DePIN P2P 加速网络 API 路由。

提供：
- /api/depina/status            查询本节点 P2P 身份与监听地址
- /api/depina/seed              导出本节点 libp2p seed（私钥，需谨慎）
- /api/depina/peers/add        把目标 multiaddr 注入本节点 peerstore
- /api/depina/peers/dial       与目标 peerId 建立 P2P 连接（QUIC/TCP/relay）
- /api/depina/peers/status     查询对端 peerId 的连接状态与已知地址
- /api/depina/peers/relay     列出当前所有 P2P 连接
- /api/depina/peers/lookup    通过公共 DHT 查询对端 peerId 的已知地址
- /api/depina/accel/echo      向对端发一条加速流（回显验证 P2P 数据面）

节点管理：
- 应用启动时由 main.py lifespan 自动拉起 sidecar；
- 若 sidecar 未运行，API 返回 503 + {error: "sidecar not running"}，
  由调用方决定是否先 POST /api/depina/node/start。

依赖：
- Go sidecar v12+（支持 add_peer/dial_peer/accel_echo/connectedness/relay_peers/dht_lookup/dump_seed）
- 环境变量 DEPINA_SIDECAR：sidecar 可执行文件路径
- 环境变量 DEPINA_DATA_DIR：节点数据目录（存 libp2p_seed.b64）
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.database import AsyncSessionLocal, get_db
from app.deps import get_current_user
from app.models import User
from app.services.billing.subscription import require_accel
from app.services.depina.sidecar_client import SidecarError, SidecarManager

logger = logging.getLogger("app.depina.api")

router = APIRouter(prefix="/depina", tags=["depina"])

# ── 单例管理 ──────────────────────────────────────────────────────────
_mgr: Optional[SidecarManager] = None


def _default_sidecar_path() -> str:
    """按平台选择 sidecar 可执行文件（环境变量 DEPINA_SIDECAR 优先）。

    Windows 开发机：depina/sidecar/test/ 下最新的 sidecar_v*_win.exe
    生产 Linux：/opt/depin/sidecar/linux-amd64/sidecar
    """
    env = os.environ.get("DEPINA_SIDECAR")
    if env:
        return env
    if sys.platform == "win32":
        backend = Path(__file__).resolve().parents[2]
        test_dir = backend.parent / "depina" / "sidecar" / "test"
        if test_dir.is_dir():
            exes = list(test_dir.glob("sidecar_v*_win.exe"))
            if exes:
                # 按修改时间取最新，避免字母序把 v2 排在 v14 之后
                return str(max(exes, key=lambda p: p.stat().st_mtime))
        return str(backend.parent / "depina" / "sidecar" / "windows-amd64" / "sidecar.exe")
    return "/opt/depin/sidecar/linux-amd64/sidecar"


def _get_mgr() -> SidecarManager:
    """惰性初始化 SidecarManager（不启动进程，只构造）。"""
    global _mgr
    if _mgr is None:
        data_dir = os.environ.get(
            "DEPINA_DATA_DIR", str(Path(__file__).resolve().parents[2] / "data")
        )
        _mgr = SidecarManager(_default_sidecar_path(), data_dir)
    return _mgr


def _ensure_running(mgr: SidecarManager) -> None:
    if not mgr.is_running():
        raise HTTPException(503, detail="sidecar not running; call POST /api/depina/node/start first")


# ── 应用生命周期集成 ──────────────────────────────────────────────────


async def start_sidecar_quietly() -> None:
    """应用启动时自动拉起 sidecar；失败只告警，不阻塞主服务启动。

    由环境变量 DEPINA_AUTOSTART 控制（默认开启；设为 0/false/no 关闭）。
    """
    if os.environ.get("DEPINA_AUTOSTART", "1").lower() in ("0", "false", "no"):
        logger.info("depina sidecar autostart disabled")
        return
    mgr = _get_mgr()
    try:
        await mgr.start()
        st = await mgr.status()
        logger.info(
            "depina sidecar ready peer=%s addrs=%s",
            st.get("peerId"),
            st.get("listenAddrs"),
        )
    except Exception as e:  # noqa: BLE001 — 自动启动失败不应影响主服务
        logger.warning("depina sidecar 自动启动失败（不影响主服务）: %s", e)


async def stop_sidecar_quietly() -> None:
    """应用退出时停止 sidecar；失败只告警。"""
    try:
        await _get_mgr().stop()
    except Exception as e:  # noqa: BLE001
        logger.warning("depina sidecar 停止失败: %s", e)


# ── 请求/响应模型 ─────────────────────────────────────────────────────


class PeerAddReq(BaseModel):
    """add_peer：注入一个 multiaddr 到 peerstore。"""

    target: str = Field(..., description="multiaddr，含 /p2p/<peerId> 段，如 /ip4/1.2.3.4/tcp/4001/p2p/12D3Koo...")


class PeerDialReq(BaseModel):
    """dial_peer：与目标 peerId 建立 P2P 连接。"""

    target: str = Field(..., description="目标 peerId（如 12D3Koo...）")
    timeout_ms: int = Field(15000, ge=1000, le=60000, description="拨号超时（毫秒）")


class PeerLookupReq(BaseModel):
    """dht_lookup：通过 DHT 查询目标 peerId 的已知地址。"""

    target: str = Field(..., description="目标 peerId")
    timeout_ms: int = Field(15000, ge=1000, le=60000, description="DHT 查询超时（毫秒）")


class AccelEchoReq(BaseModel):
    """accel_echo：向对端发一条加速流（回显验证）。"""

    target: str = Field(..., description="目标 peerId")
    payload_hex: str = Field(..., description="payload 的 hex 编码")
    timeout_ms: int = Field(10000, ge=1000, le=60000, description="单次回显超时（毫秒）")


class ConnectednessReq(BaseModel):
    """connectedness：查询对端连接状态。"""

    target: str = Field(..., description="目标 peerId")


class SidecarStatus(BaseModel):
    running: bool
    pid: Optional[int] = None
    peer_id: Optional[str] = None
    listen_addrs: list[str] = []


# ── v15 新增：开通开关 + 反代 + 贡献 模型 ──────────────────────────────


class ProxyEnableReq(BaseModel):
    """set_proxy_enabled：切换本节点是否接受网页端 HTTP 反代请求。"""

    enabled: bool = Field(..., description="true 开启反代，false 关闭")
    allowed_hosts: list[str] = Field(
        default_factory=list,
        description="源站白名单（主机名/IP 列表）；空 = 只允许 127.0.0.1/localhost",
    )


class HttpProxyReq(BaseModel):
    """http_proxy：让对端节点反代某个 URL。"""

    target: str = Field(..., description="目标节点 peerId")
    url: str = Field(..., description="完整源站 URL，如 http://1.2.3.4:8080/page")
    timeout_ms: int = Field(20000, ge=1000, le=60000, description="单次反代超时（毫秒）")


class RegistryRegisterReq(BaseModel):
    """registry/register：客户端节点向中心注册（开通加速功能时调用）。"""

    ip: str = Field("", description="本节点公网/内网 IP")
    proxy_enabled: bool = Field(True, description="是否开通加速服务（注册即开通）")
    allowed_hosts: list[str] = Field(default_factory=list, description="可服务的源站白名单")
    user_id: Optional[str] = Field(None, description="关联账号（MVP 可空）")


class RegistryContributeReq(BaseModel):
    """registry/contribute：节点贡献上报（body 传参，避免 query 与 GET 混用）。"""

    peer_id: str = Field(..., description="节点 peerId")
    served_bytes: int = Field(..., ge=0, description="本次加速服务字节数")
    peer_count: int = Field(1, ge=1, description="本次服务的网页端数量")


class RegistryNearbyReq(BaseModel):
    """registry/nearby：网页端按 URL 找就近加速节点。"""

    url: str = Field(..., description="要访问的源站 URL")


# ── 节点生命周期 ──────────────────────────────────────────────────────


@router.get("/node/status", response_model=SidecarStatus)
async def node_status():
    """查询 sidecar 运行状态（不要求 sidecar 在跑也能调）。"""
    mgr = _get_mgr()
    if not mgr.is_running():
        return SidecarStatus(running=False)
    try:
        st = await mgr.status()
        return SidecarStatus(
            running=True,
            pid=mgr.get_pid(),
            peer_id=st.get("peerId"),
            listen_addrs=st.get("listenAddrs", []),
        )
    except SidecarError as e:
        raise HTTPException(502, detail=f"sidecar 状态查询失败: {e}")


@router.post("/node/start")
async def node_start():
    """启动 sidecar 子进程。幂等（已在跑直接返回）。"""
    mgr = _get_mgr()
    try:
        await mgr.start()
    except SidecarError as e:
        raise HTTPException(500, detail=f"sidecar 启动失败: {e}")
    st = await mgr.status()
    return {"started": True, **st}


@router.post("/node/stop")
async def node_stop():
    """停止 sidecar 子进程。"""
    mgr = _get_mgr()
    await mgr.stop()
    return {"stopped": True}


# ── 节点身份 ──────────────────────────────────────────────────────────


@router.get("/status")
async def p2p_status():
    """本节点 P2P 身份 + 监听地址。"""
    mgr = _get_mgr()
    _ensure_running(mgr)
    try:
        return await mgr.status()
    except SidecarError as e:
        raise HTTPException(502, detail=str(e))


@router.get("/seed")
async def p2p_seed():
    """导出本节点 libp2p seed（base64 Ed25519 私钥）。

    ⚠️  敏感操作：拿到 seed 等价于完全控制该节点身份。
    仅供运维/备份用途，不应暴露给前端。
    """
    mgr = _get_mgr()
    _ensure_running(mgr)
    try:
        return await mgr.dump_seed()
    except SidecarError as e:
        raise HTTPException(502, detail=str(e))


# ── P2P 节点管理 ──────────────────────────────────────────────────────


@router.post("/peers/add")
async def peers_add(req: PeerAddReq):
    """把目标 multiaddr 注入 peerstore（同机/已知地址场景下绕过 DHT 发现）。"""
    mgr = _get_mgr()
    _ensure_running(mgr)
    try:
        return await mgr.add_peer(req.target)
    except SidecarError as e:
        raise HTTPException(400, detail=f"add_peer 失败: {e}")


@router.post("/peers/dial")
async def peers_dial(req: PeerDialReq):
    """与目标 peerId 建立 P2P 连接。"""
    mgr = _get_mgr()
    _ensure_running(mgr)
    try:
        return await mgr.dial_peer(req.target, req.timeout_ms)
    except SidecarError as e:
        raise HTTPException(502, detail=f"dial 失败: {e}")


@router.get("/peers/connectedness")
async def peers_connectedness(target: str = Query(..., description="目标 peerId")):
    """查询对端 peerId 的连接状态 + 已知地址。"""
    mgr = _get_mgr()
    _ensure_running(mgr)
    try:
        return await mgr.connectedness(target)
    except SidecarError as e:
        raise HTTPException(502, detail=str(e))


@router.get("/peers/relay")
async def peers_relay():
    """列出当前本节点所有 P2P 连接（含 relay 中转）。"""
    mgr = _get_mgr()
    _ensure_running(mgr)
    try:
        return await mgr.relay_peers()
    except SidecarError as e:
        raise HTTPException(502, detail=str(e))


@router.post("/peers/lookup")
async def peers_lookup(req: PeerLookupReq):
    """通过公共 DHT 查询对端 peerId 的已知地址。

    同机/本地 peer 通常查不到（未 announce 到公共 DHT），跨网时才能查到。
    """
    mgr = _get_mgr()
    _ensure_running(mgr)
    try:
        return await mgr.dht_lookup(req.target, req.timeout_ms)
    except SidecarError as e:
        raise HTTPException(404, detail=f"dht_lookup 失败: {e}")


# ── P2P 数据面（加速回显） ────────────────────────────────────────────


@router.post("/accel/echo")
async def accel_echo(req: AccelEchoReq):
    """向对端发一条加速流（回显验证 P2P 数据面）。

    前提：先 add_peer + dial_peer 建立连接，再发回显。
    """
    mgr = _get_mgr()
    _ensure_running(mgr)
    try:
        return await mgr.send_accel(req.target, req.payload_hex, req.timeout_ms)
    except SidecarError as e:
        raise HTTPException(502, detail=f"accel_echo 失败: {e}")


# ── v15 新增：开通开关 + HTTP 反代 + 贡献 ─────────────────────────────


@router.post("/node/enable")
async def node_enable(
    req: ProxyEnableReq,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """开通加速服务：本节点开始接受网页端 HTTP 反代请求（set_proxy_enabled）。

    鉴权：必须登录，且通过 require_accel（订阅含 depin 权益，或余额 > 0 按量付费）。
    关闭开关（enabled=False）不校验，便于用户随时关闭。
    """
    if req.enabled:
        await require_accel(db, user)
    mgr = _get_mgr()
    _ensure_running(mgr)
    try:
        res = await mgr.set_proxy_enabled(req.enabled)
    except SidecarError as e:
        raise HTTPException(502, detail=f"set_proxy_enabled 失败: {e}")
    return {
        "enabled": req.enabled,
        "user_id": user.id,
        "allowed_hosts": req.allowed_hosts,
        "result": res,
    }


@router.post("/accel/proxy")
async def accel_proxy(req: HttpProxyReq):
    """HTTP 反代数据面：让对端节点作为临时加速服务器，代拉取 req.url。

    网页端流程：先 registry/nearby 选出节点 → 本端点让该节点反代源站 →
    节点从源站拉回响应经 P2P 回流。成功后中心侧建议调 registry/contribute
    记录贡献。
    """
    mgr = _get_mgr()
    _ensure_running(mgr)
    try:
        return await mgr.http_proxy(req.target, req.url, req.timeout_ms)
    except SidecarError as e:
        raise HTTPException(502, detail=f"http_proxy 失败: {e}")


@router.get("/node/contribution")
async def node_contribution():
    """查询本节点贡献统计（served_bytes/served_peers，MVP 来自 registry 记录）。"""
    from app.database import AsyncSessionLocal
    from app.models_depina import DepinaNode
    from sqlalchemy import select

    mgr = _get_mgr()
    if not mgr.is_running():
        return {"peer_id": None, "served_bytes": 0, "served_peers": 0}
    st = await mgr.status()
    pid = st.get("peerId")
    async with AsyncSessionLocal() as session:
        row = (await session.execute(select(DepinaNode).where(DepinaNode.peer_id == pid))).scalar_one_or_none()
    return {
        "peer_id": pid,
        "served_bytes": row.served_bytes if row else 0,
        "served_peers": row.served_peers if row else 0,
    }


# ── v15 新增：中心节点注册表 + 就近选路 ──────────────────────────────


@router.post("/registry/register")
async def registry_register(
    req: RegistryRegisterReq,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """客户端节点向中心注册（开通加速功能时调用 + 定期心跳）。

    鉴权：必须登录；proxy_enabled=True 时须通过 require_accel（订阅含 depin 或余额>0）。
    写入 depina_nodes 表并绑定 user_id（users.id int）；重复 peer_id 幂等更新 + 刷新 last_seen_at。
    若本节点 sidecar 在跑且 req.proxy_enabled，自动把节点反代开关也打开。
    """
    from app.services.depina import registry as reg

    if req.proxy_enabled:
        await require_accel(db, user)

    # 顺带把本地 sidecar 的反代开关切到一致（sidecar 在跑时）
    mgr = _get_mgr()
    st = None
    if mgr.is_running():
        try:
            st = await mgr.status()
        except SidecarError:
            st = None
    if st and req.proxy_enabled:
        try:
            await mgr.set_proxy_enabled(True)
        except SidecarError as e:
            logger.warning("register 时开反代失败: %s", e)
    peer_id = st.get("peerId") if st else ""
    if not peer_id:
        raise HTTPException(400, detail="本节点 sidecar 未运行，无法注册；先 POST /node/start")
    ip = req.ip or _public_ip_hint()
    row = await reg.register_node(
        peer_id=peer_id,
        ip=ip,
        listen_addrs=st.get("listenAddrs", []),
        proxy_enabled=req.proxy_enabled,
        allowed_hosts=req.allowed_hosts,
        user_id=int(user.id),
    )
    return {"registered": True, "user_id": user.id, "node": reg.node_to_dict(row)}


@router.post("/registry/nearby")
async def registry_nearby(req: RegistryNearbyReq):
    """网页端：按目标 URL 找就近的已开通加速节点。

    返回 {"node": {...}}（可用节点）或 {"node": null}（无可用节点，回退直连源站）。
    网页端拿到节点后，通过该节点的 sidecar http_proxy 走 P2P 拉取资源。
    """
    from app.services.depina import registry as reg

    node = await reg.find_nearby_node(req.url)
    return {"url": req.url, "node": reg.node_to_dict(node) if node else None}


@router.post("/registry/contribute")
async def registry_contribute(
    req: RegistryContributeReq,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    """节点贡献上报（body 传参）+ 贡献奖励入账（单向激励模型）。

    鉴权：节点 owner 登录（get_current_user）；开通加速须通过 require_accel。
    单向激励（v15.x）：反代流量对用户侧一律免费——
      - 订阅用户（plus/pro）：每日 accel_gb_per_day 配额内免费，超额 403 拒绝（次日 UTC 0 点恢复）
      - 余额用户（无订阅）：不限量免费（不扣 payg），仅节点侧赚贡献奖励
    入账：节点侧按 served_bytes × ACCEL_REWARD_FEN_PER_KB（1分/KB=1元/GB）调
    settlement.credit_topup 计入 node owner 的 users.balance_fen，并累计
    depina_nodes.reward_fen。用户侧钱包不因使用而减少，仅因贡献而增加。
    """
    from app.models_depina import DepinaNode
    from app.services.billing.settlement import credit_topup
    from app.services.depina import registry as reg
    from sqlalchemy import select

    # 鉴权：节点须属于当前用户
    async with AsyncSessionLocal() as session:
        row = (
            (await session.execute(select(DepinaNode).where(DepinaNode.peer_id == req.peer_id)))
            .scalar_one_or_none()
        )
    if row is None:
        raise HTTPException(404, detail="节点未注册，无法记贡献")
    if row.user_id is not None and int(row.user_id) != int(user.id):
        raise HTTPException(403, detail="不能为他人的节点记贡献")

    # 单向激励模型（v15.x）：反代流量对用户侧一律免费。
    # - 订阅用户（plus/pro）：每日配额内免费；超额 403 拒绝（次日 UTC 0 点恢复）
    # - 余额用户（无订阅）：配额=0 视为不限量免费（不扣 payg），仅节点侧赚贡献奖励
    # 节点侧贡献奖励照旧入账 users.balance_fen + depina_nodes.reward_fen（节点赚加速费）
    accel = await require_accel(db, user)
    daily_used = await reg.get_daily_usage(user.id)
    quota_mb = int(accel.get("daily_quota_mb") or 0)
    quota_bytes = quota_mb * 1024 * 1024
    is_sub = str(accel.get("reason", "")).startswith("subscription")
    new_used = int(daily_used.get("served_bytes", 0)) + req.served_bytes

    if is_sub and quota_bytes > 0 and new_used > quota_bytes:
        over = new_used - quota_bytes
        raise HTTPException(
            403,
            detail=(
                f"超出每日加速配额：{quota_mb} MB/日已用 "
                f"{daily_used.get('served_bytes', 0)}B，本次 {req.served_bytes}B 超额 {over}B；次日 0 点 UTC 恢复"
            ),
        )

    # 记节点贡献 + 当日用量（与 credit_topup 同 db session 同事务，保证原子提交）
    contrib = await reg.record_contribution(req.peer_id, req.served_bytes, req.peer_count, session=db)
    usage_after = await reg.record_daily_usage(user.id, req.served_bytes, session=db)

    # 贡献奖励入账（节点侧赚钱，单向激励）
    reward_fen = contrib.get("reward_fen", 0)
    credited = False
    if reward_fen > 0:
        await credit_topup(
            db,
            user,
            reward_fen,
            ref_type="depina_contribution",
            ref_id=req.peer_id,
            note=f"DePIN 反代贡献奖励 {req.served_bytes}B（节点侧单向激励）",
        )
        credited = True
    await db.commit()

    return {
        "ok": True,
        "peer_id": req.peer_id,
        "served_bytes": req.served_bytes,
        "accel_grant": accel,
        "reward_fen": reward_fen,
        "credited": credited,
        "total_reward_fen": contrib.get("total_reward_fen"),
        "payg": {
            "bytes": 0,
            "charged_fen": 0,
            "note": "单向激励：用户侧反代免费，仅节点侧赚贡献奖励",
        },
        "daily_usage": usage_after,
    }


def _public_ip_hint() -> str:
    """尽力拿到本机出口 IP（仅用于注册表展示；NAT 后拿到的通常是内网 IP）。"""
    import socket

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"
