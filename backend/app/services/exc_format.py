"""将异常格式化为可展示、非空的错误文案。"""

from __future__ import annotations

import re

# 上游出图/出视频通道临时 5xx 故障标记：
# do_request_failed / Failed to reach upstream（上游通道不可达），或 Seedream 5xx（官方方舟上游故障）。
# 此类故障多为瞬时可重试，无需向用户暴露原始 JSON、request id，统一软化为重试提示。
_UPSTREAM_TRANSIENT_RE = re.compile(
    r"do_request_failed|Failed to reach upstream|Seedream error 5\d\d"
)

# 已建连、等响应超时（不是连不上）
_READ_TIMEOUT_EXC_NAMES = frozenset({"ReadTimeout"})
# 已建连、发请求体超时
_WRITE_TIMEOUT_EXC_NAMES = frozenset({"WriteTimeout"})
# 建连失败 / 代理不可达（含未细分的 TimeoutException）
_CONNECT_EXC_NAMES = frozenset(
    {
        "ConnectError",
        "ConnectTimeout",
        "PoolTimeout",
        "TimeoutException",
        "NetworkError",
        "ProxyError",
    }
)


def format_exception_message(
    exc: BaseException,
    *,
    fallback: str = "未知错误",
    limit: int = 500,
) -> str:
    """生成带类型名的错误文案；ConnectError 等空 message 时补上可读说明。"""
    name = type(exc).__name__
    detail = str(exc).strip()
    if name in _READ_TIMEOUT_EXC_NAMES:
        tip = detail or "上游已连通但响应超时（图片/视频生成可能超过等待上限）"
        return f"网络错误（{name}）：{tip}"[:limit]
    if name in _WRITE_TIMEOUT_EXC_NAMES:
        tip = detail or "上游已连通但发送请求超时"
        return f"网络错误（{name}）：{tip}"[:limit]
    if name in _CONNECT_EXC_NAMES:
        tip = detail or "无法连接上游服务（请检查网络、代理或 TokenFree 是否可达）"
        return f"网络错误（{name}）：{tip}"[:limit]
    # 上游通道临时故障：软化为可重试提示，隐藏原始 JSON 与 request id
    if _UPSTREAM_TRANSIENT_RE.search(detail):
        return "上游服务暂时繁忙，请稍后再试"[:limit]
    if not detail:
        return f"{name}：{fallback}"[:limit]
    if detail.startswith(name):
        return detail[:limit]
    return f"{name}: {detail}"[:limit]
