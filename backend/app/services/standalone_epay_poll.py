"""单机版 ePay 跨机支付：镜像订单注册与状态轮询（2026-10-18）。

单机版无公网 IP，ezfp.cn 异步通知打不到本机。设计：
- 下单：notify_url 指向 156 /prod-api/standalone-pay；订单号用 STD- 前缀；
  同时经 /prod-api/standalone-pay/register 在 156 写一条镜像订单（156 验签/
  轮询查状态都要用）。
- 入账：前端支付弹窗轮询本机 /api/billing/epay/notify-local/{out_trade_no}，
  本模块向 156 /prod-api/standalone-pay/status 查询；查到 paid 后由调用方
  执行与本地回调完全相同的入账逻辑（订阅激活/余额充值），幂等。
- 156 不可达：status 返回 None，本机订单保持 pending，不影响主流程。
"""
from __future__ import annotations

import logging

import httpx

from app.config import get_settings

logger = logging.getLogger("app.standalone_epay_poll")


def _service_key() -> str:
    return str(getattr(get_settings(), "log_report_key", "") or "").strip()


def _endpoint_base() -> str:
    """由 log_report_endpoint（.../prod-api/standalone-log）推导 156 prod-api base。"""
    ep = str(getattr(get_settings(), "log_report_endpoint", "") or "").strip()
    if ep.endswith("/prod-api/standalone-log"):
        return ep[: -len("/prod-api/standalone-log")]
    if "/prod-api/" in ep:
        return ep.rsplit("/prod-api/", 1)[0]
    return ""


def remote_notify_url() -> str:
    """ezfp notify 应填的 156 公网地址（单机版下单专用）。"""
    return f"{_endpoint_base()}/prod-api/standalone-pay"


async def register_remote_order(
    *, out_trade_no: str, sku_id: str, user_id: int, amount_fen: int, credit_fen: int, pay_type: str
) -> bool:
    """在 156 注册镜像订单（STANDALONE_LOG_KEY 鉴权）。失败静默，不影响下单。"""
    ep = _endpoint_base()
    key = _service_key()
    if not ep or not key:
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                ep + "/prod-api/standalone-pay/register",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "out_trade_no": out_trade_no,
                    "sku_id": sku_id,
                    "user_id": user_id,
                    "amount_fen": amount_fen,
                    "credit_fen": credit_fen,
                    "pay_type": pay_type,
                },
            )
            r.raise_for_status()
            data = r.json()
            return bool(data.get("ok", True))
    except Exception as exc:
        logger.info("standalone mirror order register to 156 failed (non-fatal): %s", exc)
        return False


async def query_remote_order_status(out_trade_no: str) -> dict | None:
    """查询 156 镜像订单支付状态；156 不可达/未注册返回 None。"""
    ep = _endpoint_base()
    key = _service_key()
    if not ep or not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                ep + "/prod-api/standalone-pay/status",
                headers={"Authorization": f"Bearer {key}"},
                json={"out_trade_no": out_trade_no},
            )
            r.raise_for_status()
            data = r.json()
            if data.get("found"):
                return {"paid": bool(data.get("paid")), "trade_no": data.get("trade_no", "")}
            return None
    except Exception as exc:
        logger.debug("standalone order status poll failed: %s", exc)
        return None


async def place_order_via_156(
    *, out_trade_no: str, name: str, money_yuan: str, pay_type: str, clientip: str = ""
) -> dict | None:
    """156 代下单（本机 ePay 直连 ezfp.cn 受本地 IP 风控时改走 156 服务端下单）。
    返回 trade_no/pay_mode/payurl/qr_payload；156 不可达或下单失败返回 None。"""
    ep = _endpoint_base()
    key = _service_key()
    if not ep or not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                ep + "/prod-api/standalone-pay/place-order",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "out_trade_no": out_trade_no,
                    "name": name,
                    "money_yuan": money_yuan,
                    "pay_type": pay_type,
                    "clientip": clientip,
                },
            )
            r.raise_for_status()
            data = r.json()
            if not data.get("ok"):
                logger.warning("156 place-order failed: %s", data.get("reason"))
                return None
            return data
    except Exception as exc:
        logger.debug("156 place-order unreachable: %s", exc)
        return None

