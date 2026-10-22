from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKEND_DIR = Path(__file__).resolve().parent.parent
_ENV_FILE = _BACKEND_DIR / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE) if _ENV_FILE.is_file() else ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "PRINTFILM"
    debug: bool = True
    # 是否打印 SQLAlchemy 原始 SQL（默认关，避免刷屏；需要排查 SQL 时设 SQL_ECHO=true）
    sql_echo: bool = False
    secret_key: str = "dev-secret-change-me"
    access_token_expire_minutes: int = 60 * 24 * 7
    # dj.aiwyq.bond「短剧制作」SSO 换票共享密钥（与 cloud_vip 8789 /m/api/sso/tick 同源）
    sso_shared_secret: str = ""
    # 单机版日志登记上报（156 只做后台日志登记，不存作品；产物全留本机）：
    # 配置 LOG_REPORT_ENDPOINT + LOG_REPORT_KEY 后任务完成/失败自动向 156 报一条元数据；
    # 任一为空则关闭上报（本地零外部依赖不变）。绝不上传任何文件/媒体字节。
    log_report_endpoint: str = ""
    log_report_key: str = ""
    log_report_timeout: float = 5.0
    # dj.aiwyq.bond 8789 登录校验通道（单机版第二身份源）；留空=用默认 https://dj.aiwyq.bond
    dj_auth_base_url: str = ""

    # 本地版默认 SQLite（零外部依赖，数据落 backend/data/printfilm.db）；需要 Postgres 时可用环境变量覆盖
    database_url: str = f"sqlite+aiosqlite:///{_BACKEND_DIR.as_posix()}/data/printfilm.db"
    database_url_sync: str = f"sqlite:///{_BACKEND_DIR.as_posix()}/data/printfilm.db"
    # Postgres 连接池（统一任务平台 / API 共用）
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_pool_recycle_sec: int = 1800
    db_pool_timeout_sec: int = 30
    redis_url: str = ""

    ark_api_key: str = ""
    ark_base_url: str = "https://new.aiwyq.bond/v1"
    # 文字模型：开源版固定 TokenFree New API，后台选模型
    openai_api_key: str = ""
    openai_base_url: str = "https://new.aiwyq.bond/v1"
    # 默认模型以站点预设为准（Agnes）；实际以后台渠道 models + 默认定稿为准
    model_llm: str = "agnes-3.0-flash"
    model_image: str = "agnes-image-2.5-flash"
    # Seedream 4.5 接入点（可选；未配则回退 model_image）
    model_image_45: str = ""
    model_video: str = "agnes-video-2.5-flash"
    # Seedance 2.0 接入点（可选；未配则仅使用 MODEL_VIDEO）
    model_video_2: str = ""
    # Seedance 2.5 官方范围约 4–30 秒
    seedance_duration_min: int = 4
    seedance_duration_max: int = 30
    model_audio: str = "qwen-tts-2025-05-22"
    # Agnes AI 引擎；开源版统一走 new.aiwyq.bond New API 网关（与 ark/openai 同址）。
    # 用户需自行到 new.aiwyq.bond 控制台申请 key：
    #   1. 打开 https://new.aiwyq.bond/console，邮箱注册登录
    #   2. 控制台首页 → API Keys → 创建/复制 API key（sk- 开头）
    #   3. 将 key 填入 backend/.env 的 AGNES_API_KEY / ARK_API_KEY / OPENAI_API_KEY，
    #      或写入 backend/data/local_api_key.txt（单机版运行时会同步到 .env）
    agnes_base_url: str = "https://new.aiwyq.bond/v1"
    agnes_api_key: str = ""
    # 逗号分隔多账号 key 池（429 自动轮换；N 把 key ≈ N× 吞吐）
    agnes_api_keys: str = ""
    # dj 站点 cloud_vip 的 cloud.db 路径（只读）：aidj 新用户注册后按 email 直读
    # agnes_vip 表的免费 Agnes key 自动绑定；留空则自动绑定关闭（仍可手动同步）
    agnes_vip_db_path: str = ""
    agnes_text_model: str = "agnes-3.0-flash"
    agnes_image_model: str = "agnes-image-2.5-flash"
    agnes_video_model: str = "agnes-video-2.5-flash"
    agnes_video_poll_interval: float = 10.0
    agnes_video_poll_timeout: float = 1800.0
    # 视频提交遇 429/队列满时轮换不同账号的重试轮数（每轮换下一把 key；
    # 免费池实测约 1/4 key 可 200，轮数越多命中率越高；每轮含 1 RPM 节流与退避）
    agnes_video_retry_rounds: int = 8
    # 上游容量已满（429 饱和/视频队列满）时视频任务后台退避重排队：
    # 开启后提交命中容量错误不判终态，保持 pending + next_action_at 指数退避，
    # 命中 agnes 容量后自动继续生成；用户可随时取消（全额退款）
    video_capacity_retry_enabled: bool = True
    video_capacity_backoff_base_sec: int = 90
    video_capacity_backoff_factor: int = 3
    video_capacity_backoff_max_sec: int = 1800
    # 启动时一次性回收存量「429 容量失败/串行级联取消」的视频任务进后台重排队队列（幂等）
    video_capacity_reclaim_stalled: bool = True
    # 容量感知排队（CapacityRadar）：用真实提交结果推断上游视频容量，三态 OPEN/CLOSED/PROBING。
    # CLOSED=有容量（放行排队任务 + 短退避 + 内部多轮换 key）；OPEN=饱和（长退避 + 视频任务不租约，
    # 仅队头 probe 任务放行）；PROBING=冷却后探路（队头任务提前提交当探针）。零额外探测请求。
    video_capacity_adaptive_enabled: bool = True
    # 探测间隔：冷却进入 PROBING 的秒数，以及探针失败后下次探针间隔（连败按 ×1.5 递增，封顶）
    video_capacity_probe_interval_sec: int = 30
    video_capacity_probe_max_interval_sec: int = 180
    # 距上次成功提交超过该分钟数 → CLOSED 自动转 PROBING（主动探路）
    video_capacity_stale_probe_min: int = 5
    # OPEN（饱和）长退避：base × factor^n，封顶（默认 120s → 180 → 270 → … → 600s）
    video_capacity_open_backoff_base_sec: int = 120
    video_capacity_open_backoff_factor: float = 1.5
    video_capacity_open_backoff_max_sec: int = 600
    # CLOSED（有容量）短退避：容量释放后放行排队任务的重排队间隔
    video_capacity_closed_backoff_sec: int = 3
    # 饱和期（OPEN/PROBING）探针头多槽付费快车道：付费（订阅/余额>0）任务并行提交当探针，
    # 免费保留兜底槽；默认 4+1，与全局 Worker 槽 task_runtime_max_concurrency 对齐
    video_capacity_paid_probe_slots: int = 4
    video_capacity_free_probe_slots: int = 1
    # 全局视频提交速率门控（次/秒，令牌桶）：0=不限速。防止容量刚释放被积压任务瞬时打满
    video_capacity_submit_rate: float = 0.0
    # Agnes 出口代理（本机 IP 代理池）：
    #  - 静态代理 AGNES_HTTP_PROXY，如 http://127.0.0.1:8888（企业代理池统一入口）
    #  - 动态池 AGNES_PROXY_POOL_URL（jhao104 兼容 API，如 http://127.0.0.1:5010）：
    #    每次请求 /get/ 取一个健康出口（127.0.0.1:5399x），请求结束 /delete/ 归还；
    #    动态池开启时静态代理不生效。两者都空 = 直连。
    agnes_http_proxy: str = ""
    agnes_proxy_pool_url: str = "http://127.0.0.1:5010"
    agnes_proxy_pool_timeout: float = 5.0

    # 豆包语音（openspeech）— 与方舟 ARK_API_KEY 不同产品线
    volc_tts_app_id: str = ""
    volc_tts_access_key: str = ""
    volc_tts_resource_id: str = "seed-tts-2.0"
    volc_tts_speaker: str = "zh_female_cancan_uranus_bigtts"
    volc_tts_url: str = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
    # 新版控制台 API Key（与 app_id/access_key 二选一，优先 api_key）
    volc_tts_api_key: str = ""
    # 音色设计：控制台购买的 S_ 槽位，逗号分隔；配了且鉴权齐全则漫剧走 voice_design
    volc_tts_voice_design_url: str = "https://openspeech.bytedance.com/api/v3/tts/voice_design"
    volc_tts_voice_design_speaker_ids: str = ""
    # Seedream: 2k|3k|4k or WIDTHxHEIGHT，且总像素 >= 3686400（约 2560x1440）
    ark_image_size: str = "2k"
    ark_video_resolution: str = "480p"
    ark_video_ratio: str = "16:9"
    ark_video_poll_interval: float = 8.0
    ark_video_poll_timeout: float = 900.0
    # Parallel generation concurrency (per project)
    pipeline_image_concurrency: int = 3
    # TokenFree / New API 同时观察的生图任务上限（超出会 429）
    tokenfree_image_concurrency: int = 1
    # Seedance 2.5 官方并发上限约 10
    pipeline_video_concurrency: int = 10
    pipeline_audio_concurrency: int = 4
    # 单用户漫剧视频并发上限（同时 submit/awaiting_poll）；超出部分保持 pending 排队
    drama_user_video_job_limit: int = 12
    # 单个分镜视频最大尝试次数；超过后直接失败，避免长时间卡在同一镜
    drama_fragment_max_attempts: int = 3
    # 科普 full：Seedance 视频内置口播+环境音，后期不再叠外部 TTS
    kepu_seedance_native_audio: bool = True
    # 仅当 native_audio=false 时生效：Seedance 只要操作音效，口播改后期 TTS（默认开）
    kepu_seedance_sfx_audio: bool = True

    ark_mock: bool = False
    # 内置任务平台的进程内并发上限（全站 Worker 槽位）。
    task_runtime_max_concurrency: int = 4
    # 单用户同时进行中的 Worker 槽位（不含 awaiting_poll 注册项）。
    task_user_max_concurrency: int = 4
    # Selector 每轮并发非阻塞查询上游的上限（类似 NIO select 就绪 channel 批处理）。
    task_poll_max_concurrency: int = 20
    # 孤儿恢复：leased/running 超过该秒数无更新、且本进程无执行协程时重排队。
    task_runtime_recover_grace_sec: int = 30
    # 运行中每隔多少秒扫描一次孤儿任务（调度 tick 内执行）。
    task_runtime_orphan_check_sec: int = 30
    # 调度 tick 心跳超过该秒数未刷新 → 看门狗软重启调度循环。
    task_runtime_tick_stale_sec: int = 60
    # Selector 心跳超过该秒数未刷新 → 看门狗软重启 poller。
    task_poll_stale_sec: int = 600
    # 看门狗检查间隔（秒）。
    task_runtime_watchdog_interval_sec: float = 5.0

    max_shot_duration: int = 30
    default_preview_resolution: str = "480p"
    new_user_quota: int = 5
    # Legacy flag; prefer billing_enabled
    quota_enabled: bool = False

    # Token 计费：用户扣费 = TokenFree 官方成本（billing_markup 保留兼容，不再乘）
    billing_enabled: bool = False
    billing_markup: float = 1.0
    # 平台固定价目（估算/实扣同口径）：开启后用户扣费不再按上游成本，改按下方三档
    billing_use_platform_pricing: bool = True
    # LLM：billing_llm_fen_per_500 分 / 500 tokens（ceil 零头）；默认 0.01 元/500 = 1 分/500
    billing_llm_fen_per_500: int = 1
    # 图片：每张 billing_image_fen_per_image 分；默认 0.03 元/张（0.025 元进位到 3 分）
    billing_image_fen_per_image: int = 3
    # 视频：每秒 billing_video_fen_per_sec 分（ceil）；默认 0.01 元/秒 = 1 分/秒
    billing_video_fen_per_sec: int = 1
    # 平台价目下 TTS 语音合成是否免费（默认免费）
    billing_tts_free: bool = True
    # token / 视频时长估价缓冲；按张生图官价不再乘此系数（否则 5 元赠金冻不住一张图）
    billing_estimate_buffer: float = 1.2
    # Yuan per million tokens (provider cost)
    billing_seedance_video0: float = 46.0
    billing_seedance_video1: float = 28.0
    billing_llm_per_m: float = 5.0
    billing_seedream_per_m: float = 8.0
    billing_tts_per_m: float = 2.0
    # Kie：1 credit 折合人民币分（约 $0.005 ≈ ¥0.035 → 3.5）
    billing_kie_fen_per_credit: float = 3.5
    # TokenFree / New API：quota→USD→人民币（500000 quota = 1 USD）
    billing_usd_cny: float = 7.0
    # Fallback tokens when API omits usage
    billing_est_llm_tokens: int = 80_000
    # Seedream / gpt-image 无 quota 时按张价结算，不再用 4.5 万 token × 8 元/百万
    billing_est_seedream_tokens: int = 45_000
    billing_est_tts_tokens: int = 5_000
    billing_est_seedance_tokens_per_sec: int = 32_000
    # 新用户注册专属额度（分）：进独立 grant 池，优先扣，grant 天数未用完清零
    billing_signup_grant_fen: int = 1000
    billing_signup_grant_days: int = 7

    # 用户消费里程碑弹窗（累计扣费每达 interval 分提醒一次；默认 10000 = ¥100）
    billing_user_alert_enabled: bool = True
    billing_user_alert_interval_fen: int = 10000

    # 平台总费用邮件告警（按上游 cost_fen 聚合）
    billing_admin_cost_alert_enabled: bool = False
    billing_admin_cost_alert_threshold_fen: int = 0
    billing_admin_cost_alert_emails: str = ""
    billing_admin_cost_alert_period: str = "monthly"
    billing_admin_cost_alert_last_period_key: str = ""
    billing_admin_cost_alert_last_level: int = 0

    # SMTP（管理员费用告警邮件）
    smtp_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_use_tls: bool = True

    # Epay (pay.gitcc.com)
    epay_api_url: str = "https://pay.gitcc.com"
    epay_pid: str = ""
    epay_key: str = ""
    epay_notify_url: str = ""
    epay_return_url: str = ""

    public_base_url: str = "http://127.0.0.1:8000"
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"

    tos_endpoint: str = ""
    tos_bucket: str = ""
    tos_access_key: str = ""
    tos_secret_key: str = ""
    cdn_base: str = "http://localhost:8000/static"

    # Aliyun OSS — 成片/分镜上传；FFmpeg 仍读本地文件
    oss_enabled: bool = False
    oss_endpoint: str = "oss-cn-beijing.aliyuncs.com"
    oss_region: str = "cn-hangzhou"
    oss_bucket: str = ""
    oss_folder: str = "kepu"
    oss_access_key_id: str = ""
    oss_access_key_secret: str = ""
    # 可选自定义域名；空则用 https://{bucket}.{endpoint}
    oss_public_base: str = ""
    # 生成链路：先落盘返回 /static，再入队异步上传并回填 OSS URL
    oss_upload_async: bool = True
    oss_upload_queue: str = "oss"

    # 存储提供方分发：aliyun（默认，走 oss2）/ qiniu（七牛 Kodo）。
    # oss_enabled=true 时按此选择驱动；调用方仍统一走 oss.py 公共 API。
    storage_provider: str = "aliyun"
    # 七牛 Kodo（storage_provider=qiniu 时生效）
    qiniu_access_key: str = ""
    qiniu_secret_key: str = ""
    # 空间（bucket）名
    qiniu_bucket: str = ""
    # 上传区域：east / north / south / us / auto（auto 由 SDK 探测）
    qiniu_region: str = "auto"
    # 公共访问/CDN 域名（含 scheme，如 https://cdn.example.com）；
    # 空则退化为 https://{bucket}.qiniudn.com（仅供测试，生产必须配 CDN 域名）
    qiniu_public_base: str = ""

    cors_origins: str = (
        "http://localhost:5173,http://127.0.0.1:5173,"
        "http://localhost:5174,http://127.0.0.1:5174"
    )
    # Comma-separated emails promoted to admin on startup (existing users only)
    admin_bootstrap_emails: str = ""


def _local_fallback() -> Settings:
    """本地零依赖模式默认值：DATABASE_URL / DATABASE_URL_SYNC 为空（或 .env 写空）时，
    统一回退到本机 SQLite 文件（backend/data/printfilm.db）。"""
    base = Settings()
    if not (base.database_url or "").strip():
        base = base.model_copy(
            update={
                "database_url": f"sqlite+aiosqlite:///{_BACKEND_DIR.as_posix()}/data/printfilm.db",
                "database_url_sync": f"sqlite:///{_BACKEND_DIR.as_posix()}/data/printfilm.db",
            }
        )
    return base


@lru_cache
def get_settings() -> Settings:
    base = _local_fallback()
    try:
        from app.services.model_settings import get_overlay_dict

        overlay = get_overlay_dict()
        if overlay:
            return base.model_copy(update=overlay)
    except Exception:  # noqa: BLE001
        pass
    return base


def reload_settings() -> Settings:
    get_settings.cache_clear()
    try:
        from app.services.oss import reset_oss_client

        reset_oss_client()
    except Exception:  # noqa: BLE001
        pass
    return get_settings()
