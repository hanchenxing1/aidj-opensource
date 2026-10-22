"""整部剧本导入 API：上传/粘贴整部剧本 → 建项目 → 入队自动解析+改写任务。"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models import User
from app.models_drama import DramaProject, DramaScript
from app.services.drama.script_import import (
    MAX_IMPORT_TEXT_CHARS,
    MIN_IMPORT_TEXT_CHARS,
    extract_script_text,
)
from app.services.drama.script_import_jobs import dispatch_script_import_job
from app.services.drama.workflow import build_project_params
from app.services.billing.http import http_exception_for_value_error

logger = logging.getLogger("app.drama.import_script")
router = APIRouter()

SUPPORTED_SUFFIXES = (".txt", ".md", ".fountain", ".docx", ".pdf", ".json")


class DramaImportScriptRequest(BaseModel):
    text: str = Field(min_length=1, description="整部剧本文本")
    title: str | None = None
    episode_count: int | None = Field(default=None, ge=1, le=120, description="目标集数；缺省由 AI 自动判断")
    episode_duration_sec: int | None = Field(default=None, ge=60, le=1200, description="每集时长档 60/120/300/600；缺省自动")
    image_style_id: str | None = Field(default=None, description="画面风格ID；缺省自动挑选")


async def _create_import_project(
    db: AsyncSession,
    user: User,
    raw_text: str,
    *,
    title: str | None = None,
    episode_count: int | None = None,
    episode_duration_sec: int | None = None,
    image_style_id: str | None = None,
) -> tuple[DramaProject, int]:
    text = (raw_text or "").strip()
    if len(text) < MIN_IMPORT_TEXT_CHARS:
        raise HTTPException(status_code=400, detail=f"导入的剧本至少需要 {MIN_IMPORT_TEXT_CHARS} 字")
    if len(text) > MAX_IMPORT_TEXT_CHARS:
        raise HTTPException(status_code=400, detail=f"剧本过长（上限 {MAX_IMPORT_TEXT_CHARS} 字），请拆分后导入")

    proj_params = build_project_params(
        workflow="script",
        episode_count=episode_count or 12,
        image_style_id=image_style_id or "",
        episode_duration_sec=episode_duration_sec,
    )
    proj_params["import_script"] = {"status": "queued", "chars": len(text)}

    name = (title or "").strip() or text[:20] + ("…" if len(text) > 20 else "")
    project = DramaProject(user_id=user.id, title=name[:200], params=proj_params)
    db.add(project)
    await db.flush()
    script = DramaScript(
        project_id=project.id,
        name=name[:200],
        source=text,
        params={
            "episode_count": episode_count or 12,
            "image_style_id": image_style_id or "",
            "episode_duration_sec": episode_duration_sec,
            "import_status": "parsing",
            "summary_status": "pending",
            "episode_content_status": "pending",
            "workflow": "script",
        },
    )
    db.add(script)
    await db.commit()

    try:
        task_id = await dispatch_script_import_job(
            db,
            user,
            project.id,
            episode_count=episode_count,
            episode_duration_sec=episode_duration_sec,
            image_style_id=image_style_id,
        )
    except ValueError as exc:
        await db.rollback()
        raise http_exception_for_value_error(exc) from exc

    await db.refresh(project)
    logger.info("整部剧本导入入队 project_id=%s task_id=%s chars=%s", project.id, task_id, len(text))
    return project, task_id


@router.post("/import_script", status_code=202)
async def import_script(
    body: DramaImportScriptRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """JSON 文本导入整部剧本（粘贴场景）。"""
    project, task_id = await _create_import_project(
        db,
        user,
        body.text,
        title=body.title,
        episode_count=body.episode_count,
        episode_duration_sec=body.episode_duration_sec,
        image_style_id=body.image_style_id,
    )
    return {
        "ok": True,
        "queued": True,
        "task_id": task_id,
        "project_id": project.id,
        "title": project.title,
    }


@router.post("/import_script/upload", status_code=202)
async def import_script_upload(
    file: UploadFile = File(...),
    episode_count: int | None = Form(default=None, ge=1, le=120),
    episode_duration_sec: int | None = Form(default=None, ge=60, le=1200),
    image_style_id: str | None = Form(default=None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """文件上传导入整部剧本（txt/md/fountain/docx/pdf/json）。"""
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="上传文件为空")
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="上传文件不能超过 20MB")
    name = (file.filename or "").lower()
    ext = re.search(r"\.([a-z0-9]+)$", name)
    ext = "." + ext.group(1) if ext else ""
    if ext and ext not in SUPPORTED_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail="暂不支持该格式，请上传 txt / md / fountain / docx / pdf / json",
        )
    try:
        text = extract_script_text(file.filename, raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not text:
        raise HTTPException(status_code=400, detail="未能从文件中提取到剧本文本")

    project, task_id = await _create_import_project(
        db,
        user,
        text,
        episode_count=episode_count,
        episode_duration_sec=episode_duration_sec,
        image_style_id=image_style_id,
    )
    return {
        "ok": True,
        "queued": True,
        "task_id": task_id,
        "project_id": project.id,
        "title": project.title,
        "filename": file.filename,
    }
