# -*- coding: utf-8 -*-
"""Token 单价与费用换算。"""
from __future__ import annotations

import math
from typing import Any

from app.config import Settings, get_settings


def _subscription_sku(
    sku_id: str,
    plan: str,
    name: str,
    amount_fen: int,
    period_days: int,
    *,
    perks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """订阅型 SKU：amount_fen 为本期价款；credit_fen=0（不进余额，续费扣款由后台任务执行）。

    perks 权益规格：推理免费；生图每日次数；视频每日优先生成时长额度（分钟），
    额度用完降级免费版视频生成（需排队）。
    """
    if perks is None:
        # plus：生图 200 次/日、视频 10 分钟/日优先额度；pro：生图 500 次/日、视频 30 分钟/日优先额度；推理均免费
        perks = (
            {
                "llm_free": True,
                "image_calls_per_day": 200,
                "video_priority_minutes_per_day": 10,
                "depin_enabled": True,
                "accel_gb_per_day": 1,            }
            if plan == "plus"
            else {
                "llm_free": True,
                "image_calls_per_day": 500,
                "video_priority_minutes_per_day": 30,
                "depin_enabled": True,
                "accel_gb_per_day": 10,            }
        )
    return {
        "id": sku_id,
        "kind": "subscription",
        "name": name,
        "amount_fen": amount_fen,
        "credit_fen": 0,
        "plan": plan,
        "period_days": period_days,
        "perks": perks,
    }


# SKU 清单：
#  - kind=topup：一次性充值包，支付成功入账余额（credit_fen 可含赠送）
#  - kind=subscription：订阅档位，支付成功激活/延长订阅；之后每个周期结束由
#    后台续订任务（app/services/billing/subscription.py）自动从余额扣本期价款，
#    余额不足时订阅降级为 free
# 自定义充值：单一充值 SKU，金额由下单请求 amount_fen 决定（分），到账=充值金额
TOPUP_CUSTOM_MIN_FEN = 1000  # ¥10
TOPUP_CUSTOM_MAX_FEN = 500000  # ¥5000

SKUS: list[dict[str, Any]] = [
    {
        "id": "topup_custom",
        "kind": "topup",
        "name": "余额充值",
        "custom_amount": True,
        "min_amount_fen": TOPUP_CUSTOM_MIN_FEN,
        "max_amount_fen": TOPUP_CUSTOM_MAX_FEN,
        "amount_fen": TOPUP_CUSTOM_MIN_FEN,
        "credit_fen": TOPUP_CUSTOM_MIN_FEN,
        "recommended": True,
    },
    # Plus 订阅：推理免费 / 生图 200 次/日 / 视频 10 分钟/日优先生成额度
    _subscription_sku("sub_plus_day", "plus", "Plus 日卡 ¥2", 200, 1),
    _subscription_sku("sub_plus_week", "plus", "Plus 周卡 ¥12", 1200, 7),
    _subscription_sku("sub_plus_month", "plus", "Plus 月卡 ¥45", 4500, 30),
    _subscription_sku("sub_plus_year", "plus", "Plus 年卡 ¥399", 39900, 365),
    # Pro 订阅：推理免费 / 生图 500 次/日 / 视频 30 分钟/日优先生成额度
    _subscription_sku("sub_pro_day", "pro", "Pro 日卡 ¥5", 500, 1),
    _subscription_sku("sub_pro_week", "pro", "Pro 周卡 ¥30", 3000, 7),
    _subscription_sku("sub_pro_month", "pro", "Pro 月卡 ¥100", 10000, 30),
    _subscription_sku("sub_pro_year", "pro", "Pro 年卡 ¥999", 99900, 365),
]

ORDER_EXPIRE_SECONDS = 300

# Kie 1 credit ≈ $0.005；按约 7 CNY/USD 折合 ¥0.035 ≈ 3.5 分（可配置）
DEFAULT_KIE_FEN_PER_CREDIT = 3.5

def platform_pricing_active(settings: Settings | None = None) -> bool:
    """是否启用平台固定价目（估算/实扣同口径，忽略上游成本）。"""
    s = settings or get_settings()
    return bool(getattr(s, "billing_use_platform_pricing", False))


def _duration_sec_from_raw(raw_usage: dict[str, Any] | None) -> float:
    """从 usage raw 解析视频时长（秒）。record_seedance_video_usage 写入 duration_sec。"""
    if not isinstance(raw_usage, dict):
        return 0.0
    usage = raw_usage.get("usage") if isinstance(raw_usage.get("usage"), dict) else raw_usage
    for key in ("duration_sec", "seconds", "duration"):
        try:
            v = float(usage.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if v > 0:
            return v
    return 0.0


def charge_fen_platform(
    billing_key: str,
    *,
    tokens: int = 0,
    model: str = "",
    size: str = "",
    duration_sec: float = 0.0,
    settings: Settings | None = None,
) -> int:
    """平台固定价目下的用户扣费（分）。估算/实扣共用同一公式。"""
    s = settings or get_settings()
    cap = billing_key_to_capability(billing_key)
    if cap == "llm":
        t = max(0, int(tokens))
        if t <= 0:
            return 0
        unit = max(0, int(getattr(s, "billing_llm_fen_per_500", 1)))
        return max(1, math.ceil(t / 500.0) * unit) if unit > 0 else 0
    if cap == "image":
        return max(0, int(getattr(s, "billing_image_fen_per_image", 3)))
    if cap == "video":
        d = max(float(duration_sec or 0), 2.0)
        unit = max(0, int(getattr(s, "billing_video_fen_per_sec", 1)))
        return max(1, math.ceil(d) * unit) if unit > 0 else 0
    if cap == "tts":
        if getattr(s, "billing_tts_free", True):
            return 0
    cost, charge = charge_fen_for_tokens(max(0, int(tokens)), billing_key, settings=s)
    return charge




def provider_yuan_per_m(billing_key: str, settings: Settings | None = None) -> float:
    s = settings or get_settings()
    table = {
        "seedance2:video0": s.billing_seedance_video0,
        "seedance2:video1": s.billing_seedance_video1,
        "llm_chat": s.billing_llm_per_m,
        "seedream": s.billing_seedream_per_m,
        "tts": s.billing_tts_per_m,
    }
    return float(table.get(billing_key, s.billing_llm_per_m))


def kie_fen_per_credit(settings: Settings | None = None) -> float:
    s = settings or get_settings()
    raw = getattr(s, "billing_kie_fen_per_credit", None)
    try:
        value = float(raw if raw is not None else DEFAULT_KIE_FEN_PER_CREDIT)
    except (TypeError, ValueError):
        value = DEFAULT_KIE_FEN_PER_CREDIT
    return max(0.01, value)


def kie_credits_to_cost_fen(
    credits: Any,
    settings: Settings | None = None,
) -> int | None:
    """Kie creditsConsumed → 上游成本（分）。"""
    try:
        amount = float(credits)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    return max(1, int(math.ceil(amount * kie_fen_per_credit(settings))))


def user_charge_fen(cost_fen: int, settings: Settings | None = None) -> int:
    """用户扣费 = TokenFree / 上游成本，不再乘 markup。"""
    if cost_fen <= 0:
        return 0
    return max(1, int(cost_fen))


def charge_fen_for_tokens(
    tokens: int,
    billing_key: str,
    *,
    settings: Settings | None = None,
) -> tuple[int, int]:
    """Return (cost_fen, charge_fen)；charge 与成本相同。"""
    s = settings or get_settings()
    t = max(0, int(tokens))
    yuan_per_m = provider_yuan_per_m(billing_key, s)
    cost = math.ceil(t / 1_000_000 * yuan_per_m * 100) if t else 0
    charge = user_charge_fen(cost, s)
    if t > 0 and charge < 1:
        charge = 1
        cost = max(cost, 1)
    return cost, charge


def _has_request_tokens(block: dict[str, Any]) -> bool:
    """是否像单次调用 usage（带 token 字段），而不是账户余额。"""
    return any(
        block.get(key) is not None
        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens")
    )


def _extract_newapi_quota(data: dict[str, Any], usage: dict[str, Any]) -> Any:
    """从单次调用响应提取 New API 消耗额度（不是账户剩余）。"""
    for key in ("quota_consumed", "consumed_quota"):
        if usage.get(key) is not None:
            return usage.get(key)
        if data.get(key) is not None:
            return data.get(key)
    if usage.get("quota") is None:
        return None
    if usage is not data or _has_request_tokens(usage):
        return usage.get("quota")
    return None


def parse_upstream_cost_fen(
    data: dict[str, Any] | None,
    settings: Settings | None = None,
) -> int | None:
    """从 New API quota / 火山 usage / Kie credits 解析上游成本（分）；无则 None。"""
    if not data:
        return None
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else data
    if not isinstance(usage, dict):
        return None
    for key in ("cost_fen", "cost_cents"):
        if usage.get(key) is not None:
            try:
                return max(0, int(usage[key]))
            except (TypeError, ValueError):
                pass
    from app.services.tokenfree_usage import quota_to_cost_fen

    newapi_quota = _extract_newapi_quota(data, usage)
    if newapi_quota is not None:
        converted = quota_to_cost_fen(newapi_quota, settings)
        if converted > 0:
            return converted
    # 明确人民币字段；无 New API quota 时才把火山 cost 当人民币元
    has_quota_field = any(
        usage.get(key) is not None or data.get(key) is not None
        for key in ("quota", "quota_consumed", "consumed_quota")
    )
    yuan_keys = ("cost_yuan", "total_cost_yuan")
    if not has_quota_field:
        yuan_keys = yuan_keys + ("cost", "total_cost", "amount")
    for key in yuan_keys:
        if usage.get(key) is not None:
            try:
                return max(0, int(math.ceil(float(usage[key]) * 100)))
            except (TypeError, ValueError):
                pass
    # Kie：任务级 creditsConsumed（usage 内或顶层）
    credits = usage.get("creditsConsumed")
    if credits is None:
        credits = data.get("creditsConsumed")
    converted = kie_credits_to_cost_fen(credits, settings)
    if converted is not None:
        return converted
    return None


def _image_size_from_raw(raw_usage: dict[str, Any] | None) -> str:
    """用量里的生图清晰度；忽略 480p 等视频档。"""
    if not isinstance(raw_usage, dict):
        return ""
    usage = raw_usage.get("usage") if isinstance(raw_usage.get("usage"), dict) else {}
    for block in (raw_usage, usage):
        if not isinstance(block, dict):
            continue
        for key in ("size", "image_size"):
            text = str(block.get(key) or "").strip()
            if text:
                return text
        res = str(block.get("resolution") or "").strip()
        if res and res.lower() not in {"480p", "720p", "1080p"}:
            return res
    return ""


def charge_fen_for_usage(
    tokens: int,
    billing_key: str,
    *,
    raw_usage: dict[str, Any] | None = None,
    settings: Settings | None = None,
    model: str = "",
    size: str = "",
) -> tuple[int, int, bool]:
    """按上游实际成本或 token 用量计算 (cost_fen, charge_fen, used_upstream_cost)。

    用户扣费与 TokenFree / 上游成本相同，不再加价。
    生图无 quota 时：按张官方价，避免 8 元/百万 token 低估约十倍。
    """
    s = settings or get_settings()
    if platform_pricing_active(s):
        duration = _duration_sec_from_raw(raw_usage)
        charge = charge_fen_platform(
            billing_key,
            tokens=int(tokens),
            model=model,
            size=size or _image_size_from_raw(raw_usage),
            duration_sec=duration,
            settings=s,
        )
        return charge, charge, False
    upstream_cost = parse_upstream_cost_fen(raw_usage, settings=s)
    if upstream_cost is not None and upstream_cost > 0:
        cost = upstream_cost
        charge = user_charge_fen(cost, s)
        return cost, charge, True
    if (billing_key or "").strip() == "seedream":
        catalog = _catalog_image_fen_if_per_call(
            s, model, size=size or _image_size_from_raw(raw_usage)
        )
        if catalog is not None:
            charge = user_charge_fen(catalog, s)
            return catalog, charge, False
    cost, charge = charge_fen_for_tokens(tokens, billing_key, settings=s)
    return cost, charge, False


def _catalog_image_fen_if_per_call(settings: Settings, model: str, *, size: str = "") -> int | None:
    """gpt-image / Seedream 按张价；token 计价模型返回 None。"""
    from app.services.tokenfree_image import is_seedream_family, tokenfree_working_image_model
    from app.services.tokenfree_pricing import charge_fen_official_image, lookup_rate, resolve_billing_image_size

    raw = (model or getattr(settings, "model_image", "") or "").strip()
    mid = tokenfree_working_image_model(raw)
    if not mid:
        mid = "gpt-image-2"
    rate = lookup_rate(mid)
    if rate and rate.billing == "token":
        return None
    resolved = resolve_billing_image_size(settings, model=raw, size=size)
    if rate and rate.billing == "per_call" and rate.cny_per_call > 0:
        return charge_fen_official_image(settings, model=mid, size=resolved)
    if is_seedream_family(raw) or "gpt-image" in mid.lower():
        return charge_fen_official_image(settings, model=mid, size=resolved)
    return None


def parse_usage_dict(data: dict[str, Any] | None) -> dict[str, int]:
    if not data:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else data
    if not isinstance(usage, dict):
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion = int(
        usage.get("completion_tokens")
        or usage.get("output_tokens")
        or usage.get("generated_tokens")
        or 0
    )
    total = int(usage.get("total_tokens") or (prompt + completion) or 0)
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}


def billing_key_to_capability(billing_key: str) -> str:
    key = (billing_key or "").strip().lower()
    if key == "llm_chat":
        return "llm"
    if key == "seedream":
        return "image"
    if key.startswith("seedance"):
        return "video"
    if key == "tts":
        return "tts"
    return "other"


def billing_key_label(billing_key: str) -> str:
    cap = billing_key_to_capability(billing_key)
    labels = {"llm": "LLM 对话", "image": "图片生成", "video": "视频生成", "tts": "语音合成"}
    return labels.get(cap, billing_key or "其他")


def billing_model_rate_rows(settings: Settings | None = None) -> list[dict[str, Any]]:
    """管理端展示：推荐模型走 TokenFree /api/pricing；无缓存时仍列出短名单。"""
    from app.services.tokenfree_pricing import build_official_rate_rows, cached_rates

    return build_official_rate_rows(cached_rates(), settings)


def sku_by_id(sku_id: str) -> dict[str, Any] | None:
    for item in SKUS:
        if item["id"] == sku_id:
            return item
    return None
