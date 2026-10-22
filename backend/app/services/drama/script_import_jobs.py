"""整部剧本导入 job：解析定稿 → 逐集改写标准拍摄剧本。

单 task_type `script_import`，handler 内顺序执行：
1. LLM 解析定稿（1 次）：summary + 自动集数/时长档/风格 + 逐集标题/摘要/定位行
2. 按定位行切分整部原文为「每集原文段落」
3. 逐集用 run_episode_script_from_draft 把原文改写为标准拍摄正文（N 次 LLM，逐集计费）
4. 完成后 seed_episodes_from_script 重切分镜（复用现有管道）

前端统一轮询 script.params.summary_status / episode_content_status。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.database import AsyncSessionLocal
from app.models import User
from app.models_drama import DramaProject, DramaScript
from app.models_tasks import TaskRun
from app.services.billing import record_line
from app.services.drama.agents import (
    MAX_DRAMA_EPISODES,
    merge_episode_bodies,
    normalize_series_title,
    resolve_episode_target,
    run_episode_script_from_draft,
)
from app.services.drama.episode_duration import resolve_episode_duration_profile
from app.services.drama.jobs import _enqueue_drama_task, count_completed_episodes  # noqa: F401
from app.services.drama.script_import import (
    run_script_import_parse,
    split_script_chunks,
)

logger = logging.getLogger("app.drama.script_import_job")


async def dispatch_script_import_job(
    db,
    user: User,
    project_id: int,
    *,
    episode_count: int | None = None,
    episode_duration_sec: int | None = None,
    image_style_id: str | None = None,
) -> int:
    """入队整部剧本导入任务（解析 + 逐集改写）。"""
    task_id = await _enqueue_drama_task(
        db,
        user,
        task_type="script_import",
        project_id=project_id,
        dedupe_suffix=f"import:{project_id}",
        payload={
            "project_id": project_id,
            "episode_count": episode_count,
            "episode_duration_sec": episode_duration_sec,
            "image_style_id": image_style_id,
        },
    )
    logger.info(
        "dispatch 整部剧本导入 → task_id=%s project_id=%s ep=%s dur=%s style=%s",
        task_id, project_id, episode_count, episode_duration_sec, image_style_id,
    )
    return task_id


async def run_script_import_job(
    project_id: int,
    task_id: int | None = None,
    episode_count: int | None = None,
    episode_duration_sec: int | None = None,
    image_style_id: str | None = None,
) -> dict[str, Any]:
    """Worker：整部剧本导入解析 + 逐集改写。"""
    logger.info(
        "开始整部剧本导入 project_id=%s task_id=%s ep=%s dur=%s style=%s",
        project_id, task_id, episode_count, episode_duration_sec, image_style_id,
    )

    async with AsyncSessionLocal() as db:
        project = await db.get(
            DramaProject, project_id, options=[selectinload(DramaProject.script)]
        )
        if not project or not project.script:
            logger.warning("整部剧本导入失败：缺少剧本 project_id=%s", project_id)
            return {"ok": False, "error": "missing_script"}
        script = project.script
        source = (script.source or "").strip()
        if len(source) < 100:
            return {"ok": False, "error": "empty_source"}

        # 阶段 1：LLM 解析定稿
        params = dict(script.params or {})
        params["import_status"] = "parsing"
        params["summary_status"] = "generating"
        params["episode_content_status"] = "pending"
        script.params = params
        await db.commit()
        try:
            parsed = await run_script_import_parse(
                source,
                episode_count=episode_count,
                episode_duration_sec=episode_duration_sec,
                image_style_id=image_style_id,
                auto_select_style=not str(image_style_id or "").strip(),
            )
        except Exception as exc:  # noqa: BLE001
            p0 = dict(script.params or {})
            p0["import_status"] = "failed"
            p0["summary_status"] = "failed"
            p0["import_error"] = str(exc)[:500]
            script.params = p0
            await db.commit()
            logger.exception("整部剧本解析失败 project_id=%s err=%s", project_id, exc)
            return {"ok": False, "error": str(exc)[:500]}

        summary = parsed["summary"]
        total = min(int(parsed["episodeCount"]), MAX_DRAMA_EPISODES)
        duration = int(parsed["episodeDurationSec"])
        style = str(parsed["imageStyleId"] or "").strip()
        titles = parsed["episodeTitles"]

        # 写 summary + 预填逐集（含集摘要；body 待逐集改写）
        script.summary = summary
        existing: list[dict[str, Any]] = [
            {
                "episodeNumber": t["episodeNumber"],
                "title": t["title"],
                "summary": t.get("summary") or "",
                "body": "",
                "origin": "import",
            }
            for t in titles
            if 1 <= int(t["episodeNumber"]) <= total
        ]
        script.episode_content = {"episodes": existing}

        # 按定位行切分每集原文段落
        chunks = split_script_chunks(source, titles, total)
        chunk_map = {i + 1: chunks[i] for i in range(total)}

        # 自动风格 / 时长写回项目参数
        proj_params = dict(project.params or {})
        proj_params["episode_count"] = total
        proj_params["episode_duration_sec"] = duration
        if style:
            proj_params["image_style_id"] = style
        project.params = proj_params

        # 自动改名
        series = normalize_series_title(
            summary.get("seriesTitle") or summary.get("title") or summary.get("projectTitle")
        )
        if series:
            current = (project.title or "").strip()
            looks_default = (
                len(current) > 24
                or current in {"未命名漫剧"}
                or current.startswith(source[:20])
                or current.endswith("…")
            )
            if looks_default:
                project.title = series
                script.name = series

        p1 = dict(script.params or {})
        p1["import_status"] = "parsed"
        p1["summary_status"] = "completed"
        p1["summary_error"] = None
        p1["episode_count"] = total
        p1["episode_duration_sec"] = duration
        p1["episode_content_status"] = "generating"
        p1["episode_content_progress"] = {"done": 0, "total": total}
        p1["import_chunks"] = {str(k): v for k, v in chunk_map.items() if v}
        p1.pop("import_error", None)
        script.params = p1
        await db.commit()

        # 计费：解析定稿 1 次
        user = await db.get(User, project.user_id)
        if user:
            await record_line(
                db, user_id=user.id, project_id=None, drama_project_id=project.id,
                billing_key="llm_chat", model=get_settings().model_llm,
                estimated=True, domain="drama",
            )
            await db.commit()
        logger.info("整部剧本解析完成 project_id=%s total=%s style=%s dur=%s", project_id, total, style, duration)

    # 阶段 2：逐集改写（独立 session 循环，避免长事务）
    await _run_import_rewrite_loop(project_id, task_id, total, style, duration)

    return {"ok": True, "project_id": project_id, "total": total}


async def _run_import_rewrite_loop(
    project_id: int,
    task_id: int | None,
    total: int,
    style: str,
    duration: int,
) -> None:
    from app.services.tasks.service import append_task_event

    async def _sync(done: int, tot: int, phase: str = "generating", message: str = "") -> None:
        if not task_id:
            return
        async with AsyncSessionLocal() as tdb:
            row = await tdb.get(TaskRun, int(task_id))
            if not row or row.status not in {"leased", "running", "pending"}:
                return
            pct = 100 if done >= tot else (0 if tot <= 0 else min(99, max(1, round(100 * done / tot))))
            row.progress_percent = pct
            row.current_step_key = "episode_script"
            row.current_step_status = phase
            await append_task_event(
                tdb, int(task_id), event_type="task.progress", status=row.status,
                phase=phase, message=message,
                payload={"done": done, "total": tot, "progress_percent": pct},
            )
            await tdb.commit()

    await _sync(0, total, phase="generating", message=f"整部剧本导入：开始逐集改写 0/{total}")
    async with AsyncSessionLocal() as db:
        project = await db.get(DramaProject, project_id, options=[selectinload(DramaProject.script)])
        if not project or not project.script:
            return
        script = project.script
        summary = script.summary if isinstance(script.summary, dict) else {}
        chunks = dict(script.params or {}).get("import_chunks") or {}
        existing: list[dict[str, Any]] = []
        content = script.episode_content
        if isinstance(content, dict) and isinstance(content.get("episodes"), list):
            existing = list(content["episodes"])
        elif isinstance(content, list):
            existing = list(content)
        # 时长档正文预算
        _dur = resolve_episode_duration_profile(project.params, script.params)
        target_chars = _dur.target_chars
        min_chars = _dur.min_chars
        scene_hint = _dur.scene_hint
        creative_ctx = (script.source or "")[:1500]

        await _sync(0, total, "generating", f"整部剧本导入：开始逐集改写 0/{total}")
        guard = 0
        ok = 0
        for n in range(1, total + 1):
            try:
                draft = (chunks.get(str(n)) or "").strip()
                if len(draft) >= 20:
                    batch = await run_episode_script_from_draft(
                        summary, existing, n, draft,
                        creative=creative_ctx,
                        target_chars=target_chars, min_chars=min_chars, scene_hint=scene_hint,
                    )
                else:
                    # 切分失败/段过短：用集摘要兜底扩写
                    from app.services.drama.agents import run_episode_body_from_brief
                    item = next((x for x in existing if int(x.get("episodeNumber") or 0) == n), None) or {}
                    batch = await run_episode_body_from_brief(
                        summary, existing, n,
                        creative=str(item.get("creative") or ""),
                        summary=str(item.get("summary") or ""),
                        project_source=creative_ctx,
                        target_chars=target_chars, min_chars=min_chars, scene_hint=scene_hint,
                    )
                existing = merge_episode_bodies(existing, batch, prefer_incoming=True)
                ok += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("整部导入改写第 %d 集失败 project_id=%s err=%s", n, project_id, exc)

            script.episode_content = {"episodes": existing}
            p = dict(script.params or {})
            p["episode_content_status"] = "generating"
            p["episode_content_progress"] = {"done": ok, "total": total}
            script.params = p
            await db.commit()

            user = await db.get(User, project.user_id)
            if user:
                await record_line(
                    db, user_id=user.id, project_id=None, drama_project_id=project.id,
                    billing_key="llm_chat", model=get_settings().model_llm,
                    estimated=True, domain="drama",
                )
                await db.commit()

            await _sync(ok, total, "generating", f"整部剧本改写进度 {ok}/{total}")
            guard += 1
            if guard > max(total * 2, 24):
                break

        # 终态
        done = count_completed_episodes(existing, total)
        p = dict(script.params or {})
        p["episode_content_status"] = "completed" if done >= total else "failed"
        p["episode_content_progress"] = {"done": done, "total": total}
        p["import_status"] = "completed" if done >= total else "partial"
        p.pop("episode_content_error", None)
        script.params = p
        await db.commit()

        if done >= total:
            # 复用现有分镜种子：按标准正文重切分镜
            from app.services.drama.seed import seed_episodes_from_script
            await seed_episodes_from_script(db, project, force=False)
            await _sync(done, total, "succeeded", f"整部剧本导入完成 {done}/{total}")
            logger.info("整部剧本导入完成 project_id=%s total=%s", project_id, total)
        else:
            p = dict(script.params or {})
            p["episode_content_error"] = f"改写完成 {done}/{total} 集"
            script.params = p
            await db.commit()
            await _sync(done, total, "failed", f"整部剧本导入部分失败 {done}/{total}")
            logger.warning("整部剧本导入未完成 project_id=%s done=%s/%s", project_id, done, total)


