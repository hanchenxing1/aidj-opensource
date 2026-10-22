"""漫剧全集下载：订阅会员专属（完整性检测 + 后台打包任务 + 下载）。

普通用户不可见/不可用：所有端点要求 active 订阅（未订阅 403，detail 为
{"message","code":"no_active_subscription"}，前端据此隐藏按钮）。

设计：打包走后台任务（POST 建任务 -> 轮询进度 -> 完成后下载 zip），
避免同步大请求的超时与内存风险。视频一律回源七牛 CDN（不做 alipan
fuse 本地优先——云盘盘高 iowait 时会拖死，且本地通常没有 shot 文件）。
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models import User
from app.models_drama import (
    DramaEpisode,
    DramaEpisodeFragment,
)
from app.services.billing import get_active_subscription
from app.services.drama.access import get_owned_drama_project, get_owned_episode
from app.services.drama.generation import fragment_generation_status

logger = logging.getLogger("app.drama.collection")
router = APIRouter(tags=["drama-collection"])

MAX_FRAGMENTS = 300  # 整剧单次打包上限；超限按集打包
DOWNLOAD_CONCURRENCY = 8  # 回源并发
DOWNLOAD_TIMEOUT = 300.0  # 单文件超时
JOB_TTL = timedelta(hours=1)
ROOT = Path("/var/lib/aidj_collection")


async def _ensure_member(db: AsyncSession, user: User) -> None:
    sub = await get_active_subscription(db, int(user.id))
    if sub is None:
        raise HTTPException(
            status_code=403,
            detail={"message": "该功能仅限订阅会员使用", "code": "no_active_subscription"},
        )


def _episode_folder(ep: DramaEpisode) -> str:
    try:
        ep_no = int((ep.params or {}).get("episodeNumber") or 0)
    except (TypeError, ValueError):
        ep_no = 0
    prefix = f"第{ep_no:02d}集" if ep_no >= 1 else f"分集{ep.id}"
    name = (ep.name or f"分集{ep.id}").strip()
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()[:40] or f"分集{ep.id}"
    return f"{prefix}_{cleaned}"


def _fragment_filename(frag: DramaEpisodeFragment) -> str:
    if int(frag.sort_order or 0) > 0:
        return f"shot_{int(frag.sort_order):03d}.mp4"
    return f"shot_frag{frag.id}.mp4"


def _project_query():
    return (
        select(DramaEpisode, DramaEpisodeFragment)
        .join(DramaEpisodeFragment, DramaEpisodeFragment.episode_id == DramaEpisode.id)
        .order_by(
            DramaEpisode.id.asc(),
            DramaEpisodeFragment.sort_order.asc(),
            DramaEpisodeFragment.id.asc(),
        )
    )


@router.get("/projects/{project_id}/video_completeness")
async def project_video_completeness(
    project_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """检测整部剧分镜视频是否全部生成（仅订阅会员）。

    逐集/逐镜状态：done / running(在生成) / missing(未生成或失败)。
    """
    await _ensure_member(db, user)
    await get_owned_drama_project(db, project_id, user)

    result = await db.execute(_project_query().where(DramaEpisode.project_id == project_id))
    rows = list(result.all())

    episodes: list[dict[str, Any]] = []
    current_ep: dict[str, Any] | None = None
    total = 0
    completed = 0
    running = 0
    for ep, frag in rows:
        if current_ep is None or current_ep["id"] != ep.id:
            if current_ep is not None:
                episodes.append(current_ep)
            current_ep = {
                "id": ep.id,
                "name": ep.name or f"分集{ep.id}",
                "folder": _episode_folder(ep),
                "fragments": [],
                "completed": 0,
                "missing": 0,
                "running": 0,
            }
        total += 1
        st = fragment_generation_status(frag)
        status = str(st.get("status") or "idle").lower()
        if (frag.video or "").strip():
            bucket = "done"
        elif status in {"queued", "running", "generating"}:
            bucket = "running"
        else:
            bucket = "missing"
        if bucket == "done":
            completed += 1
        elif bucket == "running":
            running += 1
        current_ep["completed"] += bucket == "done"
        current_ep["running"] += bucket == "running"
        current_ep["missing"] += bucket == "missing"
        current_ep["fragments"].append(
            {
                "fragment_id": frag.id,
                "sort_order": int(frag.sort_order or 0),
                "status": bucket,
                "video": (frag.video or "").strip(),
            }
        )
    if current_ep is not None:
        episodes.append(current_ep)

    complete = total > 0 and running == 0 and completed == total
    return {
        "project_id": project_id,
        "complete": complete,
        "total_fragments": total,
        "completed": completed,
        "running": running,
        "missing": total - completed - running,
        "episodes": episodes,
    }


# ---------- 后台打包任务 ----------

_JOBS: dict[str, dict[str, Any]] = {}


class CollectionJobCreate(BaseModel):
    episode_id: int = Field(0, ge=0, description="0=整部剧；>0=仅该集")


def _sweep_jobs() -> None:
    now = datetime.now(UTC)
    for jid in [k for k, j in _JOBS.items() if now - j["created_at"] > JOB_TTL]:
        job = _JOBS.pop(jid, None)
        if job:
            shutil.rmtree(ROOT / jid, ignore_errors=True)


async def _run_job(job: dict[str, Any]) -> None:
    """后台：并发回源下载分镜视频 -> 打 zip。状态写回 job dict。"""
    from app.services import storage

    jid = job["job_id"]
    jobdir = ROOT / jid
    workdir = jobdir / "src"
    jobdir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)
    zip_path = jobdir / "collection.zip"
    try:
        # 每段纯数据快照（创建端点已算好 arcname/url），_run_job 不再触碰 ORM
        items: list[dict[str, Any]] = job["items"]
        total = len(items)
        workdir.mkdir(parents=True, exist_ok=True)

        async def stage_one(index: int, item: dict[str, Any]) -> tuple[str, Path | None]:
            url = item["url"]
            try:
                dest = workdir / f"{index:05d}.mp4"
                async with sem:
                    await storage.download_to(url, dest, timeout=DOWNLOAD_TIMEOUT)
                return item["arcname"], dest
            except Exception as exc:  # noqa: BLE001
                logger.warning("collection stage failed frag=%s err=%s", item.get("id"), exc)
                return item["arcname"], None

        staged: list[tuple[str, Path | None]] = []
        tasks = [stage_one(i, item) for i, item in enumerate(items)]
        for coro in asyncio.as_completed(tasks):
            name, path = await coro
            staged.append((name, path))
            job["stage_done"] = sum(1 for _, p in staged if p is not None)
            job["stage_total"] = total
            if path is None:
                job["missing"].append(name)
        job["status"] = "zipping"

        entries = sorted(
            ((n, p) for n, p in staged if p is not None),
            key=lambda x: x[0],
        )
        if not entries:
            raise RuntimeError("no fragments downloadable")
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zf:
            for name, path in entries:
                zf.write(str(path), name)
            if job["missing"]:
                zf.writestr(
                    "missing.txt",
                    "以下分镜暂时下载失败，请稍后重试：\n" + "\n".join(job["missing"]),
                )
        # 删源文件，只留 zip
        shutil.rmtree(workdir, ignore_errors=True)
        job["zip_size"] = zip_path.stat().st_size
        job["status"] = "done"
    except Exception as exc:  # noqa: BLE001
        logger.warning("collection job %s failed: %s", jid, exc)
        job["status"] = "failed"
        job["error"] = str(exc)[:300]
        shutil.rmtree(jobdir, ignore_errors=True)


@router.post("/projects/{project_id}/collection_job")
async def create_collection_job(
    project_id: int,
    body: CollectionJobCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """创建整剧/单集分镜视频打包任务（仅订阅会员）。"""
    await _ensure_member(db, user)
    project = await get_owned_drama_project(db, project_id, user)
    _sweep_jobs()

    q = _project_query().where(DramaEpisode.project_id == project_id)
    if body.episode_id > 0:
        await get_owned_episode(db, body.episode_id, user)
        q = q.where(DramaEpisode.id == body.episode_id)
    result = await db.execute(q)
    pairs = [(ep, frag) for ep, frag in list(result.all()) if (frag.video or "").strip()]
    if not pairs:
        raise HTTPException(status_code=400, detail="该剧暂无已生成的分镜视频")
    if body.episode_id == 0 and len(pairs) > MAX_FRAGMENTS:
        ep_rows = list(
            (
                await db.execute(
                    select(DramaEpisode.id, DramaEpisode.name)
                    .where(DramaEpisode.project_id == project_id)
                    .order_by(DramaEpisode.id.asc())
                )
            ).all()
        )
        raise HTTPException(
            status_code=400,
            detail={
                "code": "too_many_fragments",
                "message": f"分镜数量（{len(pairs)}）超过单次打包上限 {MAX_FRAGMENTS}，请按集下载",
                "episodes": [
                    {"episode_id": r.id, "name": r.name or f"分集{r.id}"} for r in ep_rows
                ],
            },
        )

    jid = uuid.uuid4().hex
    # 纯数据快照：arcname(含去重) + url + 元信息；后台任务不再访问 ORM 会话
    used: set[str] = set()
    items: list[dict[str, Any]] = []
    for ep, frag in pairs:
        folder = _episode_folder(ep)
        fname = _fragment_filename(frag)
        arcname = f"{folder}/{fname}"
        suffix = 1
        while arcname in used:
            arcname = f"{folder}/{fname.rsplit('.', 1)[0]}_{suffix}.mp4"
            suffix += 1
        used.add(arcname)
        items.append(
            {
                "arcname": arcname,
                "url": (frag.video or "").strip(),
                "id": frag.id,
                "sort_order": int(frag.sort_order or 0),
            }
        )
    job: dict[str, Any] = {
        "job_id": jid,
        "project_id": project_id,
        "episode_id": body.episode_id,
        "user_id": int(user.id),
        "status": "staging",
        "stage_done": 0,
        "stage_total": len(items),
        "missing": [],
        "zip_size": 0,
        "error": None,
        "created_at": datetime.now(UTC),
        "title": project.title or "漫剧",
        "items": items,
    }
    _JOBS[jid] = job
    asyncio.create_task(_run_job(job))

    return {
        "job_id": jid,
        "status": job["status"],
        "stage_total": len(items),
        "episode_id": body.episode_id,
    }


@router.get("/projects/{project_id}/collection_job/{job_id}")
async def get_collection_job(
    project_id: int,
    job_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    job = _JOBS.get(job_id)
    if job is None or job["project_id"] != project_id or int(user.id) != job["user_id"]:
        raise HTTPException(status_code=404, detail="打包任务不存在")
    return {
        "job_id": job["job_id"],
        "status": job["status"],  # staging / zipping / done / failed
        "stage_done": job["stage_done"],
        "stage_total": job["stage_total"],
        "missing": job["missing"],
        "zip_size": job["zip_size"],
        "error": job["error"],
        "episode_id": job["episode_id"],
    }


@router.get("/projects/{project_id}/collection_job/{job_id}/download")
async def download_collection_job(
    project_id: int,
    job_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FileResponse:
    job = _JOBS.get(job_id)
    if job is None or job["project_id"] != project_id or int(user.id) != job["user_id"]:
        raise HTTPException(status_code=404, detail="打包任务不存在")
    if job["status"] != "done":
        raise HTTPException(status_code=409, detail="打包尚未完成")
    zip_path = ROOT / job_id / "collection.zip"
    if not zip_path.is_file():
        raise HTTPException(status_code=404, detail="打包文件不存在或已过期")

    safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", (job["title"] or "漫剧")).strip()[:40] or "漫剧"
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    if job["episode_id"] > 0:
        disp_title = f"{safe_title}_ep{job['episode_id']}"
        ascii_name = f"p{project_id}_ep{job['episode_id']}_{stamp}.zip"
    else:
        disp_title = f"{safe_title}_全集分镜"
        ascii_name = f"p{project_id}_all_fragments_{stamp}.zip"
    disp = quote(f"{disp_title}_{stamp}.zip")

    def _cleanup() -> None:
        _JOBS.pop(job_id, None)
        shutil.rmtree(ROOT / job_id, ignore_errors=True)

    return FileResponse(
        zip_path,
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{disp}"
        },
        background=BackgroundTask(_cleanup),
    )
