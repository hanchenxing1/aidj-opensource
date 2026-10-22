from pathlib import Path
import logging
import time

from fastapi import FastAPI, HTTPException, APIRouter, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text

from app.api import billing, depina, prod_api, projects, tasks, templates, tools
from app.api import api_keys as user_api_keys
from app.api import update as update_api
from app.api.v1 import router as v1_router
from app.api.drama import router as drama_router
from app.config import get_settings
from app.database import AsyncSessionLocal, engine, init_db
from app.logging_setup import configure_logging
from app.models import Template, User
from app.services.tasks.runtime import runtime_summary, start_task_runtime, stop_task_runtime
from app.services.templates_seed import TEMPLATES

settings = get_settings()
# 业务日志 INFO；DEBUG=true 不再把根日志打成 DEBUG（避免 SQL 驱动刷屏）
configure_logging(level="INFO", sql_echo=settings.sql_echo)
logger = logging.getLogger("app.http")

app = FastAPI(title=settings.app_name, version="0.2.0")
router_standalone = APIRouter(prefix="/api/standalone", tags=["standalone"])


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> "JSONResponse":
    # 兜底：未捕获异常统一返回 JSON（而非 Starlette 默认的 "Internal Server Error" 纯文本）。
    # 前端 client.ts 会对非 JSON 响应做 JSON.parse 回退，但纯文本 500 仍可能经代理/网关
    # 原样返回，导致管理台出现 "Unexpected token I ..." 报错。这里记录堆栈并返回结构化 JSON。
    logger.exception("unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "内部服务器错误", "type": "internal_error"})

origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
# Allow LAN devices (phone/tablet) hitting Vite on private IPs
_LAN_ORIGIN_RE = (
    r"https?://("
    r"localhost|127\.0\.0\.1|"
    r"192\.168\.\d{1,3}\.\d{1,3}|"
    r"10\.\d{1,3}\.\d{1,3}\.\d{1,3}|"
    r"172\.(1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3}"
    r")(:\d+)?"
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins or ["*"],
    allow_origin_regex=_LAN_ORIGIN_RE,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    # 业务可读请求日志（跳过静态资源；高频轮询默认不打）
    path = request.url.path
    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - started) * 1000
    if path.startswith("/static"):
        return response
    # 高频轮询：成功且较快时静默，避免淹没业务日志
    is_poll = (
        path.endswith("/generate_status")
        or (request.method == "GET" and path.startswith("/api/drama/scripts/"))
        or (request.method == "GET" and path.startswith("/api/drama/assets"))
        or (request.method == "GET" and path.startswith("/api/projects/") and path.count("/") == 3)
        or (request.method == "GET" and "/api/tools/tasks/" in path)
    )
    msg = f"{request.method} {path} → {response.status_code} ({elapsed_ms:.0f}ms)"
    if response.status_code >= 400:
        logger.warning(msg)
    elif elapsed_ms >= 3000:
        logger.warning("SLOW %s", msg)
    elif is_poll and elapsed_ms < 800:
        return response
    else:
        logger.info(msg)
    return response

static_dir = Path(__file__).resolve().parent.parent / "static"
static_dir.mkdir(parents=True, exist_ok=True)
(static_dir / "templates").mkdir(exist_ok=True)
(static_dir / "mock").mkdir(exist_ok=True)
# generated 常为软链 → /mnt/alipan（云盘 fuse 挂载）：挂载未就绪时目标目录缺失，
# 普通 mkdir(exist_ok=True) 会跟随失效软链抛 FileExistsError 崩掉整个 API 启动
# （2026-10-15 崩溃循环根因）。改为软链目标缺失时自动重建目标目录。
_generated = static_dir / "generated"
if _generated.is_symlink() and not _generated.is_dir():
    _link_target = _generated.resolve()
    _link_target.mkdir(parents=True, exist_ok=True)
    logger.warning("static/generated symlink target missing, recreated: %s", _link_target)
_generated.mkdir(exist_ok=True)
(static_dir / "site").mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(templates.router, prefix="/api")
app.include_router(projects.router, prefix="/api")
app.include_router(tasks.router, prefix="/api")
app.include_router(billing.router, prefix="/api")
app.include_router(tools.router, prefix="/api")
app.include_router(user_api_keys.router, prefix="/api")
app.include_router(v1_router, prefix="/api")
app.include_router(drama_router, prefix="/api")
app.include_router(update_api.router, prefix="/api")
app.include_router(depina.router, prefix="/api")
app.include_router(prod_api.router)  # 服务间接口 /prod-api/*（独立鉴权）


# ======================================================================
# 单机版：API key 单一本地源 = data/local_api_key.txt（运行时用户输入）
# 启动时把 key 同步进 .env（AGNES/OPENAI/ARK_API_KEY）与本地 key 池，
# 使模型渠道 bootstrap 能拾取。
# 开箱即用：内置共享 TokenFree vip key（new.aiwyq.bond vip 分组），
# 本地 key 文件为空时自动注入；用户也可在 new 控制台充值/替换为自己的 key。
# ======================================================================
BUILTIN_TOKENFREE_VIP_KEY = "sk-IXdOFjRmYSzZZ0Vx9jahLkeUC6cu2HFsyAQ9XOKB1w0cNF2x"
TOKENFREE_CONSOLE_URL = "https://new.aiwyq.bond/console"


def _seed_builtin_vip_key() -> None:
    """单机版开箱即用：本地 key 文件无有效 key 时注入内置共享 vip key（幂等）。"""
    base = Path(__file__).resolve().parent.parent
    keyfile = base / "data" / "local_api_key.txt"
    lines: list[str] = []
    if keyfile.is_file():
        lines = keyfile.read_text(encoding="utf-8-sig").splitlines()
    valid = [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]
    if BUILTIN_TOKENFREE_VIP_KEY in valid:
        return  # 已有内置 key，不动用户文件
    merged = [k for k in valid if k != BUILTIN_TOKENFREE_VIP_KEY]
    content = (
        "# API key（每行一把，# 开头为注释）\n"
        + BUILTIN_TOKENFREE_VIP_KEY + "\n"
        + "\n".join(merged)
        + ("\n" if merged else "")
    )
    keyfile.parent.mkdir(parents=True, exist_ok=True)
    keyfile.write_text(content, encoding="utf-8")
    logger.info("builtin vip key seeded into local_api_key.txt")

async def _sync_local_api_keys() -> None:
    import re as _re

    from app.config import reload_settings

    base = Path(__file__).resolve().parent.parent
    keyfile = base / "data" / "local_api_key.txt"
    keys: list[str] = []
    if keyfile.is_file():
        for ln in keyfile.read_text(encoding="utf-8-sig").splitlines():
            v = ln.strip()
            if v and not v.startswith("#"):
                keys.append(v)
    val = ",".join(keys)

    # .env：管理四个 key 字段（无该字段则追加）
    envp = base / ".env"
    managed = ("AGNES_API_KEYS", "AGNES_API_KEY", "OPENAI_API_KEY", "ARK_API_KEY")
    lines = envp.read_text(encoding="utf-8").splitlines() if envp.is_file() else []
    out: list[str] = []
    seen: set[str] = set()
    for ln in lines:
        m = _re.match(r"^([A-Z0-9_]+)=", ln)
        if m and m.group(1) in managed:
            seen.add(m.group(1))
            out.append(m.group(1) + "=" + val)
            continue
        out.append(ln)
    for name in managed:
        if name not in seen:
            out.append(name + "=" + val)
    envp.write_text("\n".join(out) + "\n", encoding="utf-8")

    # 本地 key 池：与 local_api_key.txt 保持一致（池是 agnes_client 的 key 来源之一）
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    pool = {
        "version": 1,
        "cursor": 0,
        "updated_at": _dt.now(ZoneInfo("UTC")).replace(microsecond=0).isoformat(),
        "keys": [
            {
                "id": "local_user_key_" + str(i),
                "key": k,
                "status": "active",
                "note": "本机运行时输入（local_api_key.txt）",
                "added_at": None,
                "last_used_at": None,
                "last_success_at": None,
                "last_error": "",
                "fail_count": 0,
                "success_count": 0,
            }
            for i, k in enumerate(keys)
        ],
    }
    poolp = base / "data" / "agnes_key_pool.json"
    poolp.write_text(
        __import__("json").dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    reload_settings()
    try:
        from app.services import agnes_client

        agnes_client.reset_agnes_client()
    except Exception:  # noqa: BLE001
        pass
    logger.info("local api keys synced: %d key(s)", len(keys))


@app.on_event("startup")
async def on_startup() -> None:
    await init_db()
    await _apply_schema_patches()
    _seed_builtin_vip_key()
    await _sync_local_api_keys()
    async with AsyncSessionLocal() as db:
        from app.services.model_settings import load_model_settings_cache

        await load_model_settings_cache(db)
    await seed_templates()
    await bootstrap_admins()
    await seed_agent_skills()
    await bootstrap_standalone_user()
    try:
        from app.services import oss as oss_svc

        oss_svc.ensure_browser_cors()
    except Exception:  # noqa: BLE001
        pass
    await start_task_runtime()
    # 订阅续费后台循环（周期扣费/降级扫描）
    from app.services.billing.subscription import start_subscription_renewer

    await start_subscription_renewer()
    # DePIN P2P 节点：自动拉起 sidecar（失败不阻塞启动）
    await depina.start_sidecar_quietly()
    # DePIN 加速服务：默认开启（sidecar 反代 + 156 注册心跳），可被手动 toggle 关闭
    # 由环境变量 DEPINA_ACCEL_AUTOSTART 控制（默认开启；设为 0/false/no 关闭）
    import os as _os
    if _os.environ.get("DEPINA_ACCEL_AUTOSTART", "1").lower() not in ("0", "false", "no"):
        try:
            from app.services.depina import node_service

            await node_service.start_heartbeat()
        except Exception as e:  # noqa: BLE001 — 加速自动开启失败不阻塞主服务
            logging.getLogger("app.main").warning("depina 加速服务默认开启失败（不影响主服务）: %s", e)


@app.on_event("shutdown")
async def on_shutdown() -> None:
    """Release Postgres pool on uvicorn worker exit."""
    from app.database import dispose_engine

    await stop_task_runtime()
    from app.services.billing.subscription import stop_subscription_renewer

    await stop_subscription_renewer()
    await depina.stop_sidecar_quietly()
    await dispose_engine()


async def _pg_columns(conn, table: str) -> set[str]:
    """读取 information_schema（Postgres/MySQL）/ PRAGMA（SQLite）列名。"""
    if str(engine.url).startswith('sqlite'):
        result = await conn.execute(text(f'PRAGMA table_info({table})'))
        return {row[1] for row in result.fetchall()}

    url = str(engine.url)
    if url.startswith('mysql'):
        schema_sql = 'DATABASE()'
    else:
        schema_sql = "'public'"
    result = await conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = " + schema_sql + " AND table_name = :table"
        ),
        {"table": table},
    )
    return {row[0] for row in result.fetchall()}


async def _apply_schema_patches() -> None:
    """Lightweight additive migrations（仅 PostgreSQL）。"""
    async with engine.begin() as conn:
        scols = await _pg_columns(conn, "shots")
        if "segment_script" not in scols:
            await conn.execute(text("ALTER TABLE shots ADD COLUMN segment_script TEXT"))
        if "last_frame_url" not in scols:
            await conn.execute(text("ALTER TABLE shots ADD COLUMN last_frame_url VARCHAR(1024)"))
        if "video_skip_reason" not in scols:
            await conn.execute(text("ALTER TABLE shots ADD COLUMN video_skip_reason VARCHAR(32)"))

        pcols = await _pg_columns(conn, "projects")
        if "pipeline_mode" not in pcols:
            await conn.execute(text("ALTER TABLE projects ADD COLUMN pipeline_mode VARCHAR(32) DEFAULT 'full'"))
        if "output_ratio" not in pcols:
            await conn.execute(text("ALTER TABLE projects ADD COLUMN output_ratio VARCHAR(16) DEFAULT ''"))
        if "voice_id" not in pcols:
            await conn.execute(text("ALTER TABLE projects ADD COLUMN voice_id VARCHAR(128) DEFAULT ''"))
        if "character_bible" not in pcols:
            await conn.execute(text("ALTER TABLE projects ADD COLUMN character_bible TEXT"))
        if "bgm_lock" not in pcols:
            await conn.execute(text("ALTER TABLE projects ADD COLUMN bgm_lock TEXT"))
        if "subtitle_preset" not in pcols:
            await conn.execute(text("ALTER TABLE projects ADD COLUMN subtitle_preset VARCHAR(32) DEFAULT ''"))
        if "style_prompt" not in pcols:
            await conn.execute(text("ALTER TABLE projects ADD COLUMN style_prompt TEXT"))
        if "character_prompt" not in pcols:
            await conn.execute(text("ALTER TABLE projects ADD COLUMN character_prompt TEXT"))
        if "extra_prompt" not in pcols:
            await conn.execute(text("ALTER TABLE projects ADD COLUMN extra_prompt TEXT"))
        if "image_model" not in pcols:
            await conn.execute(text("ALTER TABLE projects ADD COLUMN image_model VARCHAR(64) DEFAULT ''"))
        if "video_model" not in pcols:
            await conn.execute(text("ALTER TABLE projects ADD COLUMN video_model VARCHAR(64) DEFAULT ''"))

        # User billing columns
        ucols = await _pg_columns(conn, "users")
        # 早期库可能无 create_all 后缺此列（模型有、补丁曾遗漏）
        if "quota_left" not in ucols:
            await conn.execute(text("ALTER TABLE users ADD COLUMN quota_left INTEGER DEFAULT 5"))
        if "balance_fen" not in ucols:
            await conn.execute(text("ALTER TABLE users ADD COLUMN balance_fen INTEGER DEFAULT 0"))
        if "frozen_fen" not in ucols:
            await conn.execute(text("ALTER TABLE users ADD COLUMN frozen_fen INTEGER DEFAULT 0"))
        if "plan" not in ucols:
            await conn.execute(text("ALTER TABLE users ADD COLUMN plan VARCHAR(32) DEFAULT 'free'"))
        if "role" not in ucols:
            await conn.execute(text("ALTER TABLE users ADD COLUMN role VARCHAR(16) DEFAULT 'user'"))
        if "avatar_url" not in ucols:
            await conn.execute(text("ALTER TABLE users ADD COLUMN avatar_url VARCHAR(512) DEFAULT ''"))
        if "phone" not in ucols:
            await conn.execute(text("ALTER TABLE users ADD COLUMN phone VARCHAR(32) DEFAULT ''"))
        if "billing_alert_last_milestone_fen" not in ucols:
            await conn.execute(
                text("ALTER TABLE users ADD COLUMN billing_alert_last_milestone_fen INTEGER DEFAULT 0")
            )
        # 注册专属额度池（grant pool）
        if "grant_fen" not in ucols:
            await conn.execute(text("ALTER TABLE users ADD COLUMN grant_fen INTEGER NOT NULL DEFAULT 0"))
        if "frozen_grant_fen" not in ucols:
            await conn.execute(text("ALTER TABLE users ADD COLUMN frozen_grant_fen INTEGER NOT NULL DEFAULT 0"))
        if "grant_expire_at" not in ucols:
            await conn.execute(text("ALTER TABLE users ADD COLUMN grant_expire_at TIMESTAMP NULL"))

        # UsageEvent.drama_project_id for drama module billing
        uecols = await _pg_columns(conn, "usage_events")
        if "drama_project_id" not in uecols:
            await conn.execute(text("ALTER TABLE usage_events ADD COLUMN drama_project_id INTEGER"))
        if "task_run_id" not in uecols:
            await conn.execute(text("ALTER TABLE usage_events ADD COLUMN task_run_id INTEGER"))
        if "domain" not in uecols:
            await conn.execute(text("ALTER TABLE usage_events ADD COLUMN domain VARCHAR(32)"))
        if "capability" not in uecols:
            await conn.execute(text("ALTER TABLE usage_events ADD COLUMN capability VARCHAR(16)"))

        # Task platform additive columns
        trcols = await _pg_columns(conn, "task_runs")
        if "dedupe_key" not in trcols:
            await conn.execute(text("ALTER TABLE task_runs ADD COLUMN dedupe_key VARCHAR(128)"))
        if "batch_key" not in trcols:
            await conn.execute(text("ALTER TABLE task_runs ADD COLUMN batch_key VARCHAR(128)"))
        if "current_step_key" not in trcols:
            await conn.execute(text("ALTER TABLE task_runs ADD COLUMN current_step_key VARCHAR(64)"))
        if "current_step_status" not in trcols:
            await conn.execute(text("ALTER TABLE task_runs ADD COLUMN current_step_status VARCHAR(32)"))
        if "scheduled_at" not in trcols:
            await conn.execute(text("ALTER TABLE task_runs ADD COLUMN scheduled_at TIMESTAMP"))
        if "next_action_at" not in trcols:
            await conn.execute(text("ALTER TABLE task_runs ADD COLUMN next_action_at TIMESTAMP"))
        if "lease_token" not in trcols:
            await conn.execute(text("ALTER TABLE task_runs ADD COLUMN lease_token VARCHAR(64)"))
        if "lease_until" not in trcols:
            await conn.execute(text("ALTER TABLE task_runs ADD COLUMN lease_until TIMESTAMP"))
        if "billing_estimate_fen" not in trcols:
            await conn.execute(text("ALTER TABLE task_runs ADD COLUMN billing_estimate_fen INTEGER DEFAULT 0"))
        if "billing_charged_fen" not in trcols:
            await conn.execute(text("ALTER TABLE task_runs ADD COLUMN billing_charged_fen INTEGER DEFAULT 0"))
        if "billing_refunded_fen" not in trcols:
            await conn.execute(text("ALTER TABLE task_runs ADD COLUMN billing_refunded_fen INTEGER DEFAULT 0"))
        if "billing_status" not in trcols:
            await conn.execute(text("ALTER TABLE task_runs ADD COLUMN billing_status VARCHAR(16) DEFAULT 'none'"))
        if "billing_frozen_grant_fen" not in trcols:
            await conn.execute(text("ALTER TABLE task_runs ADD COLUMN billing_frozen_grant_fen INTEGER NOT NULL DEFAULT 0"))

        # 会员订阅表（本地 create_all 已建；Postgres 线上库走此处补丁）
        sub_cols = await _pg_columns(conn, "subscriptions")
        if not sub_cols:
            await conn.execute(
                text(
                    "CREATE TABLE subscriptions ("
                    " id SERIAL PRIMARY KEY,"
                    " user_id INTEGER NOT NULL,"
                    " sku_id VARCHAR(64) NOT NULL,"
                    " plan VARCHAR(32) NOT NULL,"
                    " status VARCHAR(16) NOT NULL DEFAULT 'active',"
                    " auto_renew BOOLEAN NOT NULL DEFAULT TRUE,"
                    " amount_fen INTEGER NOT NULL DEFAULT 0,"
                    " period_days INTEGER NOT NULL DEFAULT 30,"
                    " perks JSON NOT NULL DEFAULT '{}',"
                    " activated_from_order VARCHAR(64),"
                    " expires_at TIMESTAMP NOT NULL,"
                    " last_renewed_at TIMESTAMP,"
                    " lapsed_at TIMESTAMP,"
                    " cancelled_at TIMESTAMP,"
                    " created_at TIMESTAMP NOT NULL,"
                    " updated_at TIMESTAMP NOT NULL"
                    ")"
                )
            )
            for sub_col in ("user_id", "status", "sku_id", "plan"):
                await conn.execute(text(f"CREATE INDEX ix_subscriptions_{sub_col} ON subscriptions ({sub_col})"))
        # DePIN 节点注册表（v15）：create_all 已建表；此处补齐 reward_fen 列
        dn_cols = await _pg_columns(conn, "depina_nodes")
        if dn_cols:
            if "reward_fen" not in dn_cols:
                await conn.execute(text("ALTER TABLE depina_nodes ADD COLUMN reward_fen INTEGER NOT NULL DEFAULT 0"))
        # DePIN 加速每日配额使用量表（v15）：Postgres 线上库兜底建表
        accel_usage_cols = await _pg_columns(conn, "depina_accel_usage")
        if not accel_usage_cols:
            await conn.execute(
                text(
                    "CREATE TABLE depina_accel_usage ("
                    " id SERIAL PRIMARY KEY,"
                    " user_id INTEGER NOT NULL,"
                    " day DATE NOT NULL,"
                    " served_bytes BIGINT NOT NULL DEFAULT 0,"
                    " payg_bytes BIGINT NOT NULL DEFAULT 0,"
                    " payg_fen BIGINT NOT NULL DEFAULT 0,"
                    " CONSTRAINT uq_accel_usage_user_day UNIQUE (user_id, day)"
                    ")"
                )
            )
            await conn.execute(text("CREATE INDEX ix_accel_usage_user ON depina_accel_usage (user_id)"))
        # Drop leftover worker-era columns that block the new task platform.
        legacy_task_run_cols = (
            "execution_phase",
            "queue_name",
            "worker_task_id",
            "group_key",
            "next_poll_at",
            "lease_owner",
            "lease_expires_at",
            "attempt_count",
            "attempt_limit",
        )
        for col in legacy_task_run_cols:
            if col not in trcols:
                continue
            await conn.execute(text(f"DROP INDEX IF EXISTS ix_task_runs_{col}"))
            await conn.execute(text(f"ALTER TABLE task_runs DROP COLUMN {col}"))


async def bootstrap_admins() -> None:
    # Promote matching emails to admin (does not create users)
    raw = (settings.admin_bootstrap_emails or "").strip()
    if not raw:
        return
    emails = [e.strip().lower() for e in raw.split(",") if e.strip()]
    if not emails:
        return
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.email.in_(emails)))
        users = list(result.scalars().all())
        changed = False
        for user in users:
            if (user.role or "user") != "admin":
                user.role = "admin"
                changed = True
        if changed:
            await db.commit()


async def seed_agent_skills() -> None:
    """启动时把内置导演 Skill 同步进数据库。"""
    from app.services.agent.store import seed_builtin_skills

    async with AsyncSessionLocal() as db:
        await seed_builtin_skills(db)


# ======================================================================
# 单机版：内置本地账号（无注册/无后台），启动即建号
# ======================================================================
STANDALONE_EMAIL = "standalone@example.com"


async def bootstrap_standalone_user() -> None:
    """首次启动创建内置本地账号（admin 角色、充足额度），不覆盖已有。

    权限验证+额度同步（2026-10-18）：
    - 新用户：注入注册专属额度（grant_fen，默认 10 元 / 7 天有效），
      配合 billing_enabled=True，视频生成任务入队前经 ensure_balance_for_task 校验，
      超额度立即拦截并提示充值/订阅。
    - 历史用户（已开通订阅或余额充足）：保留原有 balance_fen/plan，不受影响。
    """
    from datetime import UTC, datetime, timedelta

    from app.models import User as _User
    from app.services.auth import get_user_by_email, hash_password, create_access_token

    s = get_settings()
    grant_fen = int(getattr(s, "billing_signup_grant_fen", 1000) or 0)
    grant_days = int(getattr(s, "billing_signup_grant_days", 7) or 0)
    grant_expire = datetime.now(UTC) + timedelta(days=grant_days) if grant_fen > 0 and grant_days > 0 else None

    async with AsyncSessionLocal() as db:
        existing = await get_user_by_email(db, STANDALONE_EMAIL)
        if existing is None:
            u = _User(
                email=STANDALONE_EMAIL,
                nickname="本机用户",
                hashed_password=hash_password("standalone"),
                quota_left=1000000,
                balance_fen=0,
                grant_fen=grant_fen,
                frozen_grant_fen=0,
                grant_expire_at=grant_expire,
                plan="free",
                role="admin",  # 单机无后台：本地账号承担全部权限（含任务接口）
            )
            db.add(u)
            await db.commit()
            await db.refresh(u)
            logger.info(
                "standalone user created: %s (id=%s) grant_fen=%s grant_expire=%s",
                STANDALONE_EMAIL, u.id, grant_fen, grant_expire,
            )
        else:
            # 历史用户补注赠额（仅当从未享受过：grant_fen=0 且无到期时间）
            if grant_fen > 0 and int(existing.grant_fen or 0) == 0 and not existing.grant_expire_at:
                existing.grant_fen = grant_fen
                existing.grant_expire_at = grant_expire
                await db.commit()
                logger.info(
                    "standalone user backfilled grant: %s grant_fen=%s expire=%s",
                    STANDALONE_EMAIL, grant_fen, grant_expire,
                )
            else:
                logger.info("standalone user exists: %s (id=%s) balance_fen=%s grant_fen=%s plan=%s",
                            STANDALONE_EMAIL, existing.id, existing.balance_fen, existing.grant_fen, existing.plan)


@router_standalone.get("/token")
async def standalone_token() -> dict:
    """免登录端点：返回内置本地账号的长期 token（单机专用）。"""
    from app.models import User as _User
    from app.services.auth import get_user_by_email, create_access_token

    async with AsyncSessionLocal() as db:
        u = await get_user_by_email(db, STANDALONE_EMAIL)
        if u is None:
            raise HTTPException(status_code=500, detail="内置账号未初始化")
        return {"email": u.email, "access_token": create_access_token(str(u.id))}


@router_standalone.post("/save-key")
async def standalone_save_key(payload: dict) -> dict:
    """免登录端点（单机专用）：保存用户从 new.aiwyq.bond/console 获取的 API key。
    写入 data/local_api_key.txt（去重追加），并立即同步 .env 与本地 key 池。
    body: {"key": "sk-..."} 或 {"keys": ["sk-a","sk-b"]}
    """
    key = str(payload.get("key") or "").strip()
    keys = [str(k).strip() for k in (payload.get("keys") or []) if str(k).strip()]
    if key:
        keys.append(key)
    if not keys:
        raise HTTPException(status_code=400, detail="缺少 key（body.key 或 body.keys）")
    base = Path(__file__).resolve().parent.parent
    keyfile = base / "data" / "local_api_key.txt"
    keyfile.parent.mkdir(parents=True, exist_ok=True)
    old: list[str] = []
    header = ""
    if keyfile.is_file():
        for ln in keyfile.read_text(encoding="utf-8-sig").splitlines():
            v = ln.strip()
            if v.startswith("#"):
                header += ln + "\n"
            elif v:
                old.append(v)
    merged: list[str] = []
    for k in old + keys:
        if k not in merged:
            merged.append(k)
    if not header:
        header = "# 在此输入 new.aiwyq.bond 申请的 API key（每行一把，# 开头为注释）\n"
    keyfile.write_text(header + "\n".join(merged) + "\n", encoding="utf-8")
    await _sync_local_api_keys()
    logger.info("standalone key saved: total %d", len(merged))
    return {"ok": True, "count": len(merged),
            "note": "已保存。重新打开应用（或 stop 后 start）后生效。"}


@router_standalone.get("/keys")
async def standalone_keys() -> dict:
    """免登录端点（单机专用）：查看本机已保存的 API key 数量与掩码。"""
    base = Path(__file__).resolve().parent.parent
    keyfile = base / "data" / "local_api_key.txt"
    keys: list[str] = []
    if keyfile.is_file():
        for ln in keyfile.read_text(encoding="utf-8-sig").splitlines():
            v = ln.strip()
            if v and not v.startswith("#"):
                keys.append(v)

    def mask(k: str) -> str:
        return k[:12] + "..." + k[-4:] if len(k) > 18 else k[:6] + "..."

    return {"count": len(keys), "masked": [mask(k) for k in keys]}


@router_standalone.get("/new-key")
async def standalone_new_key() -> dict:
    """免登录端点（单机专用）：当前绑定 new key 状态，供前端 api key 框/订阅页展示。

    返回 {configured, key, is_builtin_vip, count, console_url, topup_hint}。
    key 为本地文件第一把（明文）——单机版无多租户，仅本机前端可访问。
    """
    base = Path(__file__).resolve().parent.parent
    keyfile = base / "data" / "local_api_key.txt"
    keys: list[str] = []
    if keyfile.is_file():
        for ln in keyfile.read_text(encoding="utf-8-sig").splitlines():
            v = ln.strip()
            if v and not v.startswith("#"):
                keys.append(v)
    return {
        "configured": bool(keys),
        "key": keys[0] if keys else "",
        "is_builtin_vip": bool(keys) and keys[0] == BUILTIN_TOKENFREE_VIP_KEY,
        "count": len(keys),
        "console_url": TOKENFREE_CONSOLE_URL,
        "topup_hint": (
            "本机已绑定内置共享 vip key（new.aiwyq.bond）。"
            "如需更大额度：打开 new.aiwyq.bond 控制台，为该账号充值后即可继续使用；"
            "也可在下方粘贴自己的 key 替换。"
        ),
    }


# ======================================================================
# 单机版：账号绑定 + 权限验证/额度同步（2026-10-18）
# 绑定 156 账号（email）后，可拉取该账号在 156 侧的订阅/余额状态（保留权益）；
# 未绑定或 156 侧无订阅无余额的纯新用户，仅享本机 10 元 7 日注册赠额。
# ======================================================================

def _bind_file() -> Path:
    base = Path(__file__).resolve().parent.parent
    return base / "data" / "bound_email.txt"


@router_standalone.get("/entitlements")
async def standalone_entitlements() -> dict:
    """免登录端点（单机专用）：返回本机当前权益状态（含 156 侧同步结果）。"""
    from app.services.billing.entitlement_sync import bound_email, retention_snapshot, sync_entitlements

    bound = bound_email()

    # 若已绑定，尝试在线同步一次（失败静默，沿用最近状态）
    if bound:
        await sync_entitlements(bound)

    s = get_settings()
    grant_fen = int(getattr(s, "billing_signup_grant_fen", 1000) or 0)
    grant_days = int(getattr(s, "billing_signup_grant_days", 7) or 0)

    # 本机用户当前实际可用额度（balance + 未到期 grant）
    from app.database import AsyncSessionLocal as _A
    from app.services.auth import get_user_by_email

    async with _A() as db:
        u = await get_user_by_email(db, STANDALONE_EMAIL)
        local_balance_fen = int(u.balance_fen or 0) if u else 0
        local_grant_fen = int(u.grant_fen or 0) if u else 0
        local_plan = (u.plan or "free") if u else "free"
        # 本机保留权益：充值过/开通过（历史用户保留权益，不受赠额上限约束）
        local_retained = (local_balance_fen > 0) or (local_plan != "free")

    state = retention_snapshot(bound)
    # 保留权益 = 156 侧有权益 或 本机自身有权益（充值/开通过）
    retained = bool(state.get("retained")) or local_retained

    available = local_balance_fen + local_grant_fen
    return {
        "billing_enabled": bool(s.billing_enabled),
        "local_user": {
            "email": STANDALONE_EMAIL,
            "balance_fen": local_balance_fen,
            "grant_fen": local_grant_fen,
            "plan": local_plan,
        },
        "bound_156_email": bound,
        "entitlements": state,
        "retained": retained,
        "retained_local": local_retained,
        # 是否受赠额上限约束（纯新用户=未绑定/无保留权益/未充值）
        "grant_capped": not retained,
        "grant_quota_fen": grant_fen if not retained else 0,
        "available_total_fen": available,
        "total_available_yuan": round(available / 100, 2),
        "hint": (
            ("已保留权益（" + ("156 订阅/余额" if state.get("retained") else "本机充值/开通") + "），可正常使用。")
            if retained
            else (
                f"纯新用户：仅享本机注册赠额 ¥{grant_fen/100:.2f}（{grant_days}天有效），"
                "超额后可在下方「充值」或到 156 侧开通订阅/充值后恢复使用。"
            )
        ),
    }


@router_standalone.post("/entitlements/bind")
async def standalone_bind(payload: dict) -> dict:
    """免登录端点（单机专用）：绑定/更换 156 账号邮箱，用于拉取该账号在 156 侧的订阅/余额权益。
    绑定后立即触发一次权益同步，返回同步结果。"""
    from app.services.billing.entitlement_sync import sync_entitlements

    email = str(payload.get("email") or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="缺少有效的 156 账号邮箱")
    base = Path(__file__).resolve().parent.parent
    bindfile = base / "data" / "bound_email.txt"
    bindfile.parent.mkdir(parents=True, exist_ok=True)
    bindfile.write_text(email + "\n", encoding="utf-8")
    logger.info("standalone bound email: %s", email)
    # 绑定后立即同步一次（失败不阻断，保留既有权益快照）
    sync_result = await sync_entitlements(email)
    return {"ok": True, "bound": email, "sync": sync_result}


@router_standalone.post("/entitlements/sync")
async def standalone_sync() -> dict:
    """免登录端点（单机专用）：手动触发一次 156 权益同步（如充值/开通后刷新）。"""
    from app.services.billing.entitlement_sync import bound_email, sync_entitlements

    bound = bound_email()
    if not bound:
        raise HTTPException(status_code=400, detail="尚未绑定 156 账号")
    result = await sync_entitlements(bound)
    return result


# ----------------------------------------------------------------------
# 单机版：充值（直接落本机余额，不走 156 支付通道；单机无真实付费流）
# 2026-10-18：为演示/验证门控链路提供「充值」入口；
# 生产环境可替换为 156 支付链接（aidj.aiwyq.bond/console 充值）。
# 本机充值金额（分）= body.amount_fen，上限 ¥5000（500000 分），下限 ¥10（1000 分）。
# 写入：users.balance_fen += amount_fen；wallet_ledger 记一条充值台账。
# 充值后立即同步到 156 侧（若绑定），更新保留权益判定。
# ----------------------------------------------------------------------

@router_standalone.post("/billing/topup")
async def standalone_topup(payload: dict) -> dict:
    """免登录端点（单机专用）：本机充值（金额分→元余额）。
    body: {"amount_fen": 1000}  或  {"amount_yuan": 10.0}（二者传其一，同时传以 amount_fen 为准）
    """
    from datetime import UTC, datetime

    from app.database import AsyncSessionLocal as _A
    from app.models import User as _User, WalletLedger
    from app.services.auth import get_user_by_email

    amount_fen = int(payload.get("amount_fen") or 0)
    if amount_fen <= 0:
        try:
            amount_fen = int(round(float(payload.get("amount_yuan") or 0) * 100))
        except (TypeError, ValueError):
            amount_fen = 0
    if amount_fen < 1000:
        raise HTTPException(status_code=400, detail="单笔充值最低 ¥10（1000 分）")
    if amount_fen > 500000:
        raise HTTPException(status_code=400, detail="单笔充值最高 ¥5000（500000 分）")

    async with _A() as db:
        u = await get_user_by_email(db, STANDALONE_EMAIL)
        if u is None:
            raise HTTPException(status_code=500, detail="内置账号未初始化")
        new_balance = int(u.balance_fen or 0) + amount_fen
        u.balance_fen = new_balance
        db.add(WalletLedger(
            user_id=int(u.id),
            delta_fen=amount_fen,
            balance_after=new_balance,
            kind="topup_standalone",
            ref_type="standalone",
            ref_id=f"TOPUP{int(datetime.now(UTC).timestamp())}",
            note=f"本机直接充值 +¥{amount_fen/100:.2f}",
        ))
        await db.commit()

    # 若已绑定 156 账号，同步保留权益状态（本机充值不改变 156 侧，但本机保留权益判定已生效）
    result: dict = {
        "ok": True,
        "amount_fen": amount_fen,
        "amount_yuan": round(amount_fen / 100, 2),
        "new_balance_fen": new_balance,
        "note": "本机充值成功（不经过 156 支付通道，单机演示/验证用）",
    }
    return result


# ======================================================================
# 单机版：DePIN 加速服务开关（2026-10-18）
# - /api/standalone/accel/toggle   开/关节点加速服务（sidecar 反代 + 156 注册心跳）
# - /api/standalone/accel/status   当前加速状态（sidecar 开关 + 心跳 + 已登记节点）
# - /api/standalone/accel/reward    本机节点在 156 中心的累计贡献/奖励（分→秒视频）
# 单机免登录；节点 owner = data/bound_email.txt 绑定的 156 账号（未绑定回退内置邮箱）。
# ======================================================================

@router_standalone.post("/accel/toggle")
async def standalone_accel_toggle(payload: dict) -> dict:
    """开/关节点加速服务。body: {"enabled": true|false}。"""
    from app.services.depina import node_service

    enabled = bool(payload.get("enabled"))
    if enabled:
        return await node_service.start_heartbeat()
    return await node_service.stop_heartbeat()


@router_standalone.get("/accel/status")
async def standalone_accel_status() -> dict:
    """当前加速服务状态（sidecar 反代开关 + 心跳 task + 156 登记情况）。"""
    from app.services.depina import node_service

    st = await node_service.node_status()
    st["heartbeat_interval_sec"] = node_service.HEARTBEAT_INTERVAL_SEC
    st["owner_email"] = node_service._owner_email()
    st["accel_enabled"] = bool(st.get("proxy_enabled"))
    return st


@router_standalone.get("/accel/reward")
async def standalone_accel_reward() -> dict:
    """本机节点在 156 中心的累计贡献/奖励（1分=1秒视频时长）。"""
    from app.services.depina import node_service

    return await node_service.reward_snapshot()


app.include_router(router_standalone)  # 单机免登录端点（须在 token 端点定义后挂载）



def _publish_template_cover(cover: str, log: logging.Logger) -> str:
    """把 /static 封面发到 OSS；失败则仍返回原路径。"""
    from app.services import storage

    if not cover.startswith("/static/"):
        return cover
    local = storage.STATIC_ROOT / cover.removeprefix("/static/")
    if not local.is_file():
        log.warning("template cover missing on disk: %s", local)
        return cover
    try:
        return storage.publish_local(local, sync=True)
    except Exception:  # noqa: BLE001
        log.exception("template cover OSS publish failed: %s", local)
        return cover


async def seed_templates() -> None:
    """只插入缺失的内置模板；已有记录以管理后台为准，启动不再覆盖文案与配置。"""
    log = logging.getLogger("app.seed")
    async with AsyncSessionLocal() as db:
        for item in TEMPLATES:
            data = dict(item)
            existing = await db.get(Template, data["id"])
            cover = (data.get("preview_cover") or "").strip()
            if existing:
                # 已有模板不覆盖后台配置；仅当封面仍是本地路径时按库内路径补发 OSS
                current = (existing.preview_cover or "").strip()
                if not current.startswith(("http://", "https://")):
                    published = _publish_template_cover(current or cover, log)
                    if published.startswith(("http://", "https://")) and published != current:
                        existing.preview_cover = published
                continue
            data["preview_cover"] = _publish_template_cover(cover, log)
            db.add(Template(**data))
        await db.commit()


@app.get("/api/health")
async def health() -> dict:
    from app.config import reload_settings

    s = reload_settings()
    from app.database import pool_status

    runtime = runtime_summary()
    runtime_ok = bool(runtime.get("healthy"))
    return {
        "ok": runtime_ok,
        "ark_mock": s.ark_mock,
        "db_pool": pool_status(),
        "task_runtime": runtime,
        "models": {
            "llm": s.model_llm,
            "image": s.model_image,
            "video": s.model_video,
            "audio": s.model_audio,
        },
        "quality": {
            "image_size": s.ark_image_size,
            "video_resolution": s.ark_video_resolution,
        },
        "app": s.app_name,
    }

# ======================================================================
# 单机版：同源托管前端 dist（/ 与前端路径 → frontend/index.html；
# /api/*、/static/*、/login.html 等已被上方路由/挂载处理或按文件直接服务）
# ======================================================================
_FRONTEND_DIST = Path(__file__).resolve().parent.parent.parent / "frontend"
if _FRONTEND_DIST.is_dir():
    _FE_INDEX = _FRONTEND_DIST / "index.html"

    @app.get("/", include_in_schema=False)
    async def fe_root():
        from fastapi.responses import FileResponse

        return FileResponse(str(_FE_INDEX))

    @app.get("/{full_path:path}", include_in_schema=False)
    async def fe_spa_catchall(full_path: str):
        from fastapi.responses import FileResponse, JSONResponse as _J

        if full_path.startswith(("api/", "static/", "docs/")):
            return _J(status_code=404, content={"detail": "not found"})
        # 顶层静态文件（login.html / 404.html / favicon.svg / assets/* 等）优先按文件服务
        target = _FRONTEND_DIST / full_path
        if target.is_file():
            return FileResponse(str(target))
        # SPA fallback
        return FileResponse(str(_FE_INDEX))

    logger.info("standalone frontend dist mounted: %s", _FRONTEND_DIST)
else:
    logger.warning("frontend dist not found, API-only mode: %s", _FRONTEND_DIST)
