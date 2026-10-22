# -*- coding: utf-8 -*-
"""depina.sidecar_client — Python 侧 sidecar 进程管理与 JSON-RPC 客户端。

职责：
1. 启动/停止 Go sidecar 子进程（stdio JSON-RPC）
2. 暴露高层 API：
   - status() / send_accel() / stop()  （v1 基础）
   - add_peer() / dial_peer() / connectedness() / relay_peers()  （v12 新增，P2P 节点管理）
   - dump_seed()                        （v12 新增，导出持久化身份）
3. 节点身份：本地持久化 libp2p seed（data/libp2p_seed.b64），重启后 peer id 稳定

使用：
    from app.services.depina.sidecar_client import SidecarManager
    mgr = SidecarManager(sidecar_exe, data_dir)
    await mgr.start()
    info = await mgr.status()
    await mgr.add_peer("/ip4/1.2.3.4/tcp/4001/p2p/12D3Koo...")
    await mgr.dial_peer("12D3Koo...")
    await mgr.stop()

sidecar 要求：
- 可执行文件支持 stdio JSON-RPC，method 含 status / dump_seed / add_peer /
  dial_peer / accel_echo / connectedness / relay_peers / dht_lookup
- 启动时通过环境变量 LIBP2P_SEED 注入 Ed25519 seed（base64 32B），sidecar 据此恢复身份
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("app.depina.sidecar")

# sidecar 就绪超时（秒）；libp2p host 初始化 + relay + autonat 通常 <3s
_READY_TIMEOUT = 10.0
# 单次 RPC 响应超时
_RPC_TIMEOUT = 15.0
# P2P 拨号/回显默认超时
_P2P_TIMEOUT = 30.0


class SidecarError(RuntimeError):
    """sidecar 进程/通信错误。"""


class SidecarManager:
    """管理一个 Go sidecar 子进程的生命周期与 stdio JSON-RPC。"""

    def __init__(self, sidecar_exe: str | Path, data_dir: str | Path):
        self._exe = Path(sidecar_exe)
        self._data_dir = Path(data_dir)
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._req_id = 0
        self._lock = asyncio.Lock()
        # 持久化节点身份（Ed25519 seed，base64），重启 peer id 不变
        self._seed_file = self._data_dir / "libp2p_seed.b64"

    # ── 生命周期 ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """启动 sidecar 子进程并等待就绪。"""
        if self._proc and self._proc.returncode is None:
            return  # 已在跑
        if not self._exe.is_file():
            raise SidecarError(f"sidecar 可执行文件不存在: {self._exe}")
        self._data_dir.mkdir(parents=True, exist_ok=True)

        # 生成/加载持久化身份
        seed_b64 = self._load_or_create_seed()

        env = os.environ.copy()
        env["LIBP2P_SEED"] = seed_b64  # sidecar main.go v12 读这个恢复身份

        self._proc = await asyncio.create_subprocess_exec(
            str(self._exe),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # 启动后台 stderr 收集（排障用）
        asyncio.get_running_loop().create_task(self._drain_stderr())
        await self._wait_ready()
        logger.info("sidecar started pid=%s peer_seed=%s...", self._proc.pid, seed_b64[:8])

    async def stop(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        self._proc = None
        logger.info("sidecar stopped")

    async def _drain_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                break
            logger.warning("sidecar stderr: %s", line.decode("utf-8", "replace").strip())

    async def _wait_ready(self) -> None:
        """发一个 status RPC 探测 sidecar 是否已初始化完成。"""
        try:
            await self._rpc("status", timeout=_READY_TIMEOUT)
        except SidecarError:
            raise
        except (asyncio.TimeoutError, Exception) as e:
            raise SidecarError(f"sidecar 未就绪: {e}") from e

    # ── 生命周期查询 ──────────────────────────────────────────────

    def is_running(self) -> bool:
        """sidecar 子进程是否在运行。"""
        return self._proc is not None and self._proc.returncode is None

    def get_pid(self) -> Optional[int]:
        """返回子进程 PID（未启动返回 None）。"""
        return self._proc.pid if self._proc else None

    # ── JSON-RPC 通用层 ─────────────────────────────────────────────

    async def _rpc(self, method: str, params: Optional[dict] = None, timeout: float = _RPC_TIMEOUT) -> Any:
        """发一条 JSON-RPC 请求，返回 result（error 抛 SidecarError）。

        params: 可选 dict，原样作为 "params" 字段发给 sidecar。
        timeout: 响应等待秒数。
        """
        async with self._lock:
            if not self._proc or self._proc.returncode is not None:
                raise SidecarError("sidecar 未运行")
            self._req_id += 1
            req: dict[str, Any] = {"id": self._req_id, "method": method}
            if params is not None:
                req["params"] = params
            line = json.dumps(req) + "\n"
            assert self._proc.stdin
            self._proc.stdin.write(line.encode())
            await self._proc.stdin.drain()
            assert self._proc.stdout
            raw = await asyncio.wait_for(self._proc.stdout.readline(), timeout=timeout)
            if not raw:
                raise SidecarError("sidecar stdout 关闭")
            resp = json.loads(raw.decode())
            if resp.get("error"):
                raise SidecarError(resp["error"])
            return resp.get("result")

    # ── 高层 API：基础 ──────────────────────────────────────────────

    async def status(self) -> dict:
        """返回 {peerId, listenAddrs}。"""
        return await self._rpc("status")

    async def send_accel(self, target_peer: str, payload_hex: str, timeout_ms: int = 10000) -> dict:
        """向 target_peer 发一条加速流（回显验证）。

        Args:
            target_peer: 对端 peerId（如 12D3Koo...）
            payload_hex: payload 的 hex 编码
            timeout_ms: 单次回显超时（毫秒）

        Returns:
            sidecar 返回的 result dict（含 echo_hex / echo_len / via 等）
        """
        return await self._rpc(
            "accel_echo",
            params={"target": target_peer, "payload_hex": payload_hex, "timeout_ms": timeout_ms},
            timeout=_P2P_TIMEOUT,
        )

    # ── 高层 API：P2P 节点管理（v12 新增） ──────────────────────────

    async def add_peer(self, target_multiaddr: str) -> dict:
        """把 target 的 multiaddr（含 /p2p/<peerId> 段）手动注入 peerstore。

        用途：同机/已知地址场景下，让本节点能 dial 到对端（绕过 DHT 发现的
        "no addresses" 问题）。

        Args:
            target_multiaddr: multiaddr 字符串，如
                "/ip4/10.0.13.11/tcp/4001/p2p/12D3KooW..."
                或 "/ip4/0.0.0.0/udp/4001/quic/p2p/12D3KooW..."

        Returns:
            sidecar 返回的 result dict（含 target=peerId, addrs=[...]）
        """
        return await self._rpc("add_peer", params={"target": target_multiaddr})

    async def dial_peer(self, target_peer: str, timeout_ms: int = 15000) -> dict:
        """建立与 target_peer 的 P2P 传输层连接（QUIC/TCP/relay 兜底）。

        成功后 target_peer 进入 Connected 状态，可发 accel_echo。

        Args:
            target_peer: 对端 peerId
            timeout_ms: 拨号超时（毫秒），默认 15s

        Returns:
            sidecar 返回的 result dict（含 target, connectedness=2 表示已连接）
        """
        return await self._rpc(
            "dial_peer",
            params={"target": target_peer, "timeout_ms": timeout_ms},
            timeout=_P2P_TIMEOUT,
        )

    async def connectedness(self, target_peer: str) -> dict:
        """查询对端 peerId 的连接状态 + 已知地址。

        Returns:
            sidecar 返回的 result dict（含 target, connectedness, addrs）
            connectedness 取值（libp2p network.Connectedness）：
              0=NotConnected, 1=Connected, 2=CanConnect(已废弃),
              3=CannotConnect(已废弃), 4=Limited
        """
        return await self._rpc("connectedness", params={"target": target_peer})

    async def relay_peers(self) -> dict:
        """列出当前本节点的所有 P2P 连接（含 relay 中转）。

        Returns:
            sidecar 返回的 result dict（含 connected_peers=[peerId,...]）
        """
        return await self._rpc("relay_peers")

    async def dht_lookup(self, target_peer: str, timeout_ms: int = 15000) -> dict:
        """通过公共 DHT 查询对端 peerId 的已知地址（NAT 后节点发现公网 peer）。

        需要 DHT 网络中有 server 节点 announce 过该 peer。同机/本地 peer
        通常查不到（未 announce 到公共 DHT），跨网时才能真正查到。

        Args:
            target_peer: 要查询的对端 peerId
            timeout_ms: DHT 查询超时（毫秒）

        Returns:
            sidecar 返回的 result dict（含 found_peer, addrs=[...]）
            查不到时抛 SidecarError("dht find: ...")
        """
        return await self._rpc(
            "dht_lookup",
            params={"target": target_peer, "timeout_ms": timeout_ms},
            timeout=_P2P_TIMEOUT,
        )

    async def dump_seed(self) -> dict:
        """导出当前节点的 Ed25519 seed（base64）+ peerId。

        用途：宿主持久化身份，或备份到密钥管理。注意 seed 是私钥，
        切勿泄露。
        """
        return await self._rpc("dump_seed")

    # ── 高层 API：HTTP 反代数据面（v15 新增） ────────────────────────

    async def set_proxy_enabled(self, enabled: bool) -> dict:
        """切换本节点是否接受网页端 HTTP 反代请求（开通开关数据面）。

        Args:
            enabled: True 开启反代；False 拒绝所有反代请求

        Returns:
            sidecar 返回的 result dict（含 proxyEnabled）
        """
        return await self._rpc("set_proxy_enabled", params={"enabled": enabled})

    async def http_proxy(self, target_peer: str, url: str, timeout_ms: int = 20000) -> dict:
        """让 target_peer 节点作为反代服务器，向 url 的源站发起 HTTP GET，
        把响应经 P2P 回流到发起端。这就是"网页用户请求 → 客户端节点 →
        源站"的加速数据面。

        Args:
            target_peer: 目标节点 peerId（开通加速功能的客户端）
            url: 完整 URL（如 http://1.2.3.4:8080/page.html）；节点只转发
                白名单内（LIBP2P_PROXY_ALLOW）的主机
            timeout_ms: 单次反代超时（毫秒）

        Returns:
            sidecar 返回的 result dict，含 status/headers/body/body_b64/via/url。
            body 为字符串形式；二进制内容请解析 body_b64。
        """
        return await self._rpc(
            "http_proxy",
            params={"target": target_peer, "url": url, "timeout_ms": timeout_ms},
            timeout=_P2P_TIMEOUT,
        )

    # ── 身份持久化 ──────────────────────────────────────────────────

    def _load_or_create_seed(self) -> str:
        if self._seed_file.is_file():
            return self._seed_file.read_text().strip()
        seed = secrets.token_bytes(32)
        b64 = base64.b64encode(seed).decode()
        self._seed_file.write_text(b64)
        self._seed_file.chmod(0o600)
        return b64
