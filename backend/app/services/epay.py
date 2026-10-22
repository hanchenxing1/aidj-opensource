# -*- coding: utf-8 -*-
"""Epay (易支付) client for pay.gitcc.com — alipay / wxpay."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import random
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

# 上游易支付平台间歇抖动的瞬时故障特征（mapi code!=1 且 msg 命中其一 → 可安全重试）
_TRANSIENT_MARKERS = (
    "系统异常", "下单失败", "繁忙", "稍后", "频率", "limit", "timeout", "temp", "502", "503",
)
_MAPI_RETRY_MAX = 3  # 首次 + 最多 3 次重试
_MAPI_RETRY_BACKOFF = 1.5  # 退避基数（秒），按 1.5 * 2^n 递增 + 随机抖动


class _TransientEpayError(ValueError):
    """上游易支付平台瞬时故障（内部自动重试；重试耗尽后转为普通 ValueError 抛出）。"""


def _is_transient_epay_msg(msg: str) -> bool:
    m = msg.lower()
    return any(k.lower() in m for k in _TRANSIENT_MARKERS)


def _sign_pairs(params: dict[str, Any], key: str) -> str:
    items = []
    for k in sorted(params.keys()):
        if k in {"sign", "sign_type"}:
            continue
        v = params[k]
        if v is None or v == "":
            continue
        items.append(f"{k}={v}")
    raw = "&".join(items) + str(key)
    return hashlib.md5(raw.encode("utf-8")).hexdigest().lower()


def sign(params: dict[str, Any]) -> str:
    s = get_settings()
    return _sign_pairs(params, s.epay_key)


def verify(params: dict[str, Any]) -> bool:
    got = str(params.get("sign") or "").lower()
    if not got:
        return False
    expect = sign({k: v for k, v in params.items() if k != "sign"})
    return hmac.compare_digest(got, expect)


def build_submit_fields(
    *,
    out_trade_no: str,
    name: str,
    money_yuan: str,
    pay_type: str,
    notify_url: str | None = None,
    return_url: str | None = None,
    clientip: str | None = None,
    device: str | None = None,
) -> dict[str, str]:
    s = get_settings()
    if pay_type not in {"alipay", "wxpay"}:
        raise ValueError("pay_type must be alipay or wxpay")
    if not s.epay_pid or not s.epay_key:
        raise ValueError("易支付未配置 EPAY_PID / EPAY_KEY")
    notify = (notify_url or s.epay_notify_url or "").strip()
    ret = (return_url or s.epay_return_url or "").strip()
    # Avoid "/api/" in notify_url — pay.gitcc.com WAF blocks those payloads.
    if not notify:
        notify = f"{s.public_base_url.rstrip('/')}/epay/notify"
    if not ret:
        ret = f"{s.public_base_url.rstrip('/')}/pricing?paid=1"
    fields: dict[str, str] = {
        "pid": str(s.epay_pid),
        "type": pay_type,
        "out_trade_no": out_trade_no,
        "notify_url": notify,
        "return_url": ret,
        "name": name[:100],
        "money": money_yuan,
    }
    if clientip:
        fields["clientip"] = clientip
    # device=jump 只会返回收银台跳转 URL；扫码场景必须用 pc（勿传 jump）
    if device:
        fields["device"] = device
    fields["sign"] = sign(fields)
    fields["sign_type"] = "MD5"
    return fields


def _is_image_url(value: str) -> bool:
    return bool(value) and value.lower().startswith(("http://", "https://")) and any(
        ext in value.lower().split("?", 1)[0] for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp")
    )


def _is_epay_cashier_url(value: str, api_base: str) -> bool:
    """易支付收银台 /submit 页，扫码会打开站点而非原生支付码。"""
    v = (value or "").strip().lower()
    if not v.startswith(("http://", "https://")):
        return False
    base = (api_base or "").rstrip("/").lower()
    host = base.replace("https://", "").replace("http://", "").split("/", 1)[0]
    if host and host in v:
        return True
    return "pay.gitcc.com" in v or "/pay/submit/" in v


def submit_url(fields: dict[str, str]) -> str:
    s = get_settings()
    base = s.epay_api_url.rstrip("/") + "/submit.php"
    return f"{base}?{urlencode(fields)}"


def money_yuan_from_fen(fen: int) -> str:
    return f"{fen / 100:.2f}"


async def _mapi_post_once(client: httpx.AsyncClient, url: str, fields: dict[str, str]) -> dict[str, Any]:
    """单次 POST mapi.php 并解析；瞬时故障抛 _TransientEpayError，永久性错误抛 ValueError。"""
    resp = await client.post(url, data=fields)
    try:
        data = resp.json()
    except Exception as exc:
        snippet = (resp.text or "").replace("\n", " ")[:200]
        logger.error("epay mapi non-json status=%s body=%s", resp.status_code, snippet)
        if resp.status_code == 403 or "防火墙" in (resp.text or ""):
            raise ValueError(
                "易支付防火墙拦截（请确认 notify_url 不含 /api/ 路径）"
            ) from exc
        if 500 <= resp.status_code < 600:
            raise _TransientEpayError(f"易支付返回异常(HTTP {resp.status_code})") from exc
        raise ValueError(f"易支付返回异常(HTTP {resp.status_code})") from exc
    if int(data.get("code") or 0) != 1:
        msg = str(data.get("msg") or data.get("message") or "下单失败")
        if _is_transient_epay_msg(msg):
            raise _TransientEpayError(msg)
        raise ValueError(msg)
    return data


async def create_mapi_payment(
    *,
    out_trade_no: str,
    name: str,
    money_yuan: str,
    pay_type: str,
    clientip: str,
    notify_url: str | None = None,
    return_url: str | None = None,
) -> dict[str, Any]:
    """
    Call epay /mapi.php for QR / native pay payload.
    Returns pay_mode=qr（原生扫码）或 redirect（仅收银台 payurl，前端新开易支付站点）。

    上游平台瞬时故障（「系统异常/下单失败」等）自动重试至多 _MAPI_RETRY_MAX 次，
    退避 1.5 * 2^n 秒 + 随机抖动；重试耗尽或永久性错误时抛 ValueError（由调用方转 503）。
    """
    fields = build_submit_fields(
        out_trade_no=out_trade_no,
        name=name,
        money_yuan=money_yuan,
        pay_type=pay_type,
        notify_url=notify_url,
        return_url=return_url,
        clientip=clientip or "127.0.0.1",
        device="pc",
    )
    s = get_settings()
    url = s.epay_api_url.rstrip("/") + "/mapi.php"
    data: Any = None
    async with httpx.AsyncClient(timeout=30.0) as client:
        for attempt in range(_MAPI_RETRY_MAX):
            try:
                data = await _mapi_post_once(client, url, fields)
                break
            except _TransientEpayError as exc:
                if attempt + 1 >= _MAPI_RETRY_MAX:
                    raise ValueError(str(exc)) from exc
                delay = _MAPI_RETRY_BACKOFF * (2 ** attempt) + random.uniform(0.0, 0.5)
                logger.warning(
                    "epay mapi 瞬时故障第 %d 次重试（%.1fs 后）: %s",
                    attempt + 1, delay, str(exc)[:120],
                )
                await asyncio.sleep(delay)
            except httpx.HTTPError as exc:
                if attempt + 1 >= _MAPI_RETRY_MAX:
                    raise ValueError(f"易支付网络异常: {type(exc).__name__}") from exc
                delay = _MAPI_RETRY_BACKOFF * (2 ** attempt) + random.uniform(0.0, 0.5)
                logger.warning(
                    "epay mapi 网络异常第 %d 次重试（%.1fs 后）: %s",
                    attempt + 1, delay, str(exc)[:160],
                )
                await asyncio.sleep(delay)
    if data is None:
        raise ValueError("易支付下单失败（重试耗尽）")
    qrcode = str(data.get("qrcode") or "").strip()
    payurl = str(data.get("payurl") or "").strip()
    urlscheme = str(data.get("urlscheme") or "").strip()
    img = str(data.get("img") or data.get("code_url") or "").strip()

    qr_payload = ""
    if qrcode and not _is_epay_cashier_url(qrcode, s.epay_api_url):
        qr_payload = qrcode
    elif urlscheme:
        qr_payload = urlscheme
    elif _is_image_url(img):
        qr_payload = img

    # 有原生扫码内容 → 弹窗二维码；否则若有收银台 payurl → 前端新开易支付站点
    if qr_payload:
        pay_mode = "qr"
    elif payurl:
        pay_mode = "redirect"
        logger.info(
            "epay mapi redirect mode pay_type=%s trade_no=%s payurl=%s",
            pay_type,
            data.get("trade_no"),
            payurl[:120],
        )
    else:
        logger.warning(
            "epay mapi empty pay_type=%s trade_no=%s raw_keys=%s",
            pay_type,
            data.get("trade_no"),
            sorted(data.keys()) if isinstance(data, dict) else [],
        )
        raise ValueError(f"易支付未返回可用支付链接（{pay_type}）")

    return {
        "trade_no": str(data.get("trade_no") or ""),
        "qrcode": qrcode,
        "payurl": payurl,
        "img": img,
        "urlscheme": urlscheme,
        "qr_payload": qr_payload,
        "pay_mode": pay_mode,
        "raw": data,
    }
