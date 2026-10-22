# -*- coding: utf-8 -*-
"""服务间接口 /prod-api/*：LLM 补全 + 异步生图。

鉴权：服务间固定 key（Authorization: Bearer <PROD_API_KEY>，.env 配置；未配置=端点 503）。
档位：可选 user_id 按该用户订阅定档（提交时定档），缺省回落最早注册用户（free）。
生图为异步任务：提交立即返回 task_id，轮询 GET /prod-api/image/{task_id} 取结果。
"""
from __future__ import annotations

import hmac
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models import User
from app.models_tasks import TaskRun
from app.schemas_tasks import TaskCreateRequest

router = APIRouter(prefix="/prod-api", tags=["prod-api"])
logger = logging.getLogger("app.prod_api")


def _require_service_key(request: Request) -> None:
    expected = (get_settings().prod_api_key or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="prod-api 未启用（未配置 PROD_API_KEY）")
    auth = (request.headers.get("authorization") or "").strip()
    supplied = auth[len("Bearer "):].strip() if auth.lower().startswith("bearer ") else ""
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="服务 key 不匹配")


async def _resolve_user(db: AsyncSession, user_id: int | None) -> User:
    if user_id:
        user = await db.get(User, int(user_id))
        if user is not None:
            return user
    # 缺省回落最早注册用户（计费/用量归属）
    return (await db.execute(select(User).order_by(User.id).limit(1))).scalar_one()


class AiModelRequest(BaseModel):
    prompt: str
    model: str = ""
    system: str = ""
    temperature: float = 0.6
    max_tokens: int = 8192
    user_id: int | None = None  # 可选：按该用户订阅定档（缺省 free）


@router.post("/ai-model")
async def prod_ai_model(
    body: AiModelRequest, request: Request, db: AsyncSession = Depends(get_db)
) -> dict:
    _require_service_key(request)
    from app.services.gateway_tier import gateway_tier_scope, tier_for_user
    from app.services.llm_client import chat_completions

    user = await _resolve_user(db, body.user_id)
    tier = await tier_for_user(db, user)
    async with gateway_tier_scope(tier):
        content = await chat_completions(
            body.system or "You are a helpful assistant.",
            body.prompt,
            temperature=body.temperature,
            max_tokens=body.max_tokens,
        )
    logger.info("prod-api ai-model tier=%s user_id=%s out_len=%s", tier, user.id, len(content))
    return {"ok": True, "tier": tier, "content": content}


class ProdImageRequest(BaseModel):
    prompt: str
    negative: str = ""
    ratio: str = ""  # 1:1 / 16:9 / 9:16
    user_id: int | None = None


@router.post("/image")
async def prod_image_submit(
    body: ProdImageRequest, request: Request, db: AsyncSession = Depends(get_db)
) -> dict:
    _require_service_key(request)
    from app.services.gateway_tier import stamp_payload_tier
    from app.services.tasks.service import create_task

    user = await _resolve_user(db, body.user_id)
    payload = await stamp_payload_tier(
        db,
        user,
        {"prod_api": True, "prompt": body.prompt, "negative": body.negative, "ratio": body.ratio},
    )
    task = await create_task(
        db,
        user,
        TaskCreateRequest(domain="api", task_type="prod_image", payload=payload),
    )
    logger.info(
        "prod-api image submitted task_id=%s tier=%s user_id=%s", task.id, payload["gateway_tier"], user.id
    )
    return {"ok": True, "task_id": int(task.id), "tier": payload["gateway_tier"]}


@router.get("/image/{task_id}")
async def prod_image_status(
    task_id: int, request: Request, db: AsyncSession = Depends(get_db)
) -> dict:
    _require_service_key(request)
    task = await db.get(TaskRun, task_id)
    if task is None or task.task_type != "prod_image":
        raise HTTPException(status_code=404, detail="任务不存在")
    res = task.result_payload if isinstance(task.result_payload, dict) else {}
    if task.status == "succeeded":
        return {"task_id": task_id, "status": "succeeded", "urls": res.get("urls") or []}
    if task.status in ("failed", "cancelled"):
        return {"task_id": task_id, "status": task.status, "error": task.error_message}
    return {"task_id": task_id, "status": "running", "urls": []}
