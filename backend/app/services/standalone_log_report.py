"""AIDJ 单机版 → 156 日志登记上报（fire-and-forget，绝不上传任何文件/媒体字节）。

设计约束（2026-10-17）：
- 156 只做后台**日志登记**，不存 AIDJ 单机作品/产物；产物全部留在用户本机
  （backend/static/generated/）。
- 本模块只上报**任务/事件元数据**：user_email、event、domain、task_type、model、
  status、total_tokens、duration_ms、note、extra（JSON 键值）。
- 开关：.env 配置 LOG_REPORT_ENDPOINT（156 的 /prod-api/standalone-log 地址）
  + LOG_REPORT_KEY（Bearer，对应 156 侧 STANDALONE_LOG_KEY）；任一为空=关闭上报，
  保持单机"零外部依赖"（日志静默跳过，不影响任务主流程）。
- 可靠性：短超时（log_report_timeout，默认 5s）+ 吞掉一切异常（网络故障/401/503
  都不抛、不重试、不打爆用户网络）；只在本地 logs/ 留 debug 痕迹。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from app.config import get_settings

logger = logging.getLogger("app.standalone_log_report")


def _should_report() -> bool:
    settings = get_settings()
    endpoint = (getattr(settings, "log_report_endpoint", "") or "").strip()
    key = (getattr(settings, "log_report_key", "") or "").strip()
    return bool(endpoint and key)


async def report_standalone_log(event: dict[str, Any]) -> None:
    """上报一条单机日志元数据到 156；任何失败都静默吞掉（fire-and-forget）。"""
    if not _should_report():
        return
    settings = get_settings()
    endpoint = (settings.log_report_endpoint or "").strip().rstrip("/")
    key = (settings.log_report_key or "").strip()
    timeout = float(getattr(settings, "log_report_timeout", 5.0) or 5.0)

    body = {
        "user_email": str(event.get("user_email") or "")[:128],
        "event": str(event.get("event") or "task.completed")[:64],
        "domain": str(event.get("domain") or "")[:64],
        "task_type": str(event.get("task_type") or "")[:64],
        "model": str(event.get("model") or "")[:128],
        "status": str(event.get("status") or "")[:32],
        "total_tokens": int(event.get("total_tokens") or 0),
        "duration_ms": int(event.get("duration_ms") or 0),
        "note": str(event.get("note") or "")[:512],
        "ts": float(event.get("ts") or time.time()),
        "extra": dict(event.get("extra") or {}),
    }

    def _post() -> None:
        # 同步实现（requests/http.client 无外部依赖时退化为 urllib）；
        # 外层包 asyncio.to_thread，不阻塞事件循环。
        import urllib.request

        req = urllib.request.Request(
            endpoint,
            data=json.dumps(body, ensure_ascii=False, default=str).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp.read(4096)
        except Exception as exc:  # noqa: BLE001
            logger.debug("standalone-log report skipped (non-fatal): %s", exc)

    try:
        await asyncio.wait_for(asyncio.to_thread(_post), timeout=timeout + 3.0)
        logger.debug("standalone-log reported: %s", body.get("event"))
    except Exception:  # noqa: BLE001
        logger.debug("standalone-log report skipped (non-fatal)")


def report_task_log(task: Any) -> None:
    """任务终态（succeeded/failed/cancelled）后调用；调度一个 fire-and-forget 协程。

    task 为 app.models_tasks.TaskRun；提取元数据（绝不读 result_payload 里的文件内容，
    只取模型名等标量）。协程异常已吞，本函数可安全同步调用。
    """
    try:
        payload = task.payload if isinstance(task.payload, dict) else {}
        result = task.result_payload if isinstance(task.result_payload, dict) else {}
        event = {
            "user_email": "",  # 单机无多租户；留空由 156 归到 unknown（单机事件专用通道）
            "event": "task.completed" if task.status == "succeeded" else f"task.{task.status}",
            "domain": str(task.domain or ""),
            "task_type": str(task.task_type or ""),
            "model": str(
                result.get("model")
                or payload.get("model")
                or payload.get("image_model")
                or ""
            )[:128],
            "status": str(task.status or ""),
            "duration_ms": 0,
            "note": str(task.error_message or "")[:512] if task.status == "failed" else "",
            "ts": time.time(),
            "extra": {
                "task_id": int(task.id) if task.id is not None else 0,
                "project_id": int(task.project_id) if task.project_id else None,
                "drama_project_id": int(task.drama_project_id) if task.drama_project_id else None,
                "error_code": str(task.error_code or "")[:64] or None,
                "billing_status": str(task.billing_status or ""),
                "billing_charged_fen": int(task.billing_charged_fen or 0),
                "provider_task_id": str(task.provider_task_id or "")[:128] or None,
            },
        }
        # 耗时：started_at -> finished_at
        try:
            if task.started_at and task.finished_at:
                event["duration_ms"] = int(
                    max(0, (task.finished_at - task.started_at).total_seconds() * 1000)
                )
        except Exception:  # noqa: BLE001
            pass
        asyncio.get_event_loop().create_task(report_standalone_log(event))
    except RuntimeError:
        # 无运行中的事件循环（如同步上下文）：退化为立即 fire-and-forget 线程
        import threading

        threading.Thread(
            target=lambda: _run_coro(report_standalone_log(event)),
            daemon=True,
        ).start()
    except Exception:  # noqa: BLE001
        logger.debug("standalone-log task report skipped", exc_info=True)


def _run_coro(coro):
    """在独立线程里跑一个协程（fire-and-forget，异常吞掉）。"""
    try:
        asyncio.run(coro)
    except Exception:  # noqa: BLE001
        logger.debug("standalone-log report skipped (non-fatal)", exc_info=True)


def report_auth_log(event_type: str, user_email: str, note: str = "") -> None:
    """登录/注册事件日志登记（单机侧）：仅元数据，不存作品。"""
    event = {
        "user_email": str(user_email or "")[:128],
        "event": str(event_type)[:64],
        "domain": "",
        "task_type": "",
        "model": "",
        "status": "ok",
        "total_tokens": 0,
        "duration_ms": 0,
        "note": str(note or "")[:512],
        "ts": time.time(),
        "extra": {},
    }
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(report_standalone_log(event))
        else:
            _run_coro(report_standalone_log(event))
    except RuntimeError:
        import threading

        threading.Thread(
            target=lambda: _run_coro(report_standalone_log(event)),
            daemon=True,
        ).start()
    except Exception:  # noqa: BLE001
        logger.debug("standalone-log auth report skipped", exc_info=True)
