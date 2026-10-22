# -*- coding: utf-8 -*-
"""上游视频容量雷达（CapacityRadar）：用真实提交结果推断上游容量，驱动排队任务放行。

背景：原「固定 15s 盲重试」有两个结构性缺陷——
  1) 容量饱和期每个任务闷头烧完 8 轮内部重试才重排队，往返 8–11 分钟，且把 429
     容量错误当普通失败记入 key 池 fail_count，把健康 key 误杀成 exhausted（池越缩越小）；
  2) 调度器对上游容量零感知，容量释放瞬间积压任务同一 tick 涌进，易过冲再次打满。

本雷达把「固定间隔盲重试」换成「容量雷达 + 快车道 + 令牌桶」，核心思想是**零额外探测请求**：
把每次真实 submit 的结果当探针——
  - submit 成功      → CLOSED（有容量）：放行排队任务 + 短退避 + 内部多轮换 key + heal key 池
  - submit 命中 429 → OPEN（饱和）：长退避 + 视频任务不租约（仅队头 probe 放行）+ 令牌桶清空
  - CLOSED 冷却超 N 分钟 → PROBING：队头任务提前提交当探针（命中 429 是探针失败、成功是探针成功）

三态迁移（无后台探测协程，全部由 submit 结果与调度 tick 驱动）：
    CLOSED ──429──► OPEN
    OPEN   ──probe 成功──► CLOSED（并批量放行 + heal key 池 + 清空令牌桶）
    CLOSED ──冷却 stale──► PROBING（队头探针提前到 probe_at）
    PROBING ──probe 429──► 仍按 OPEN 长退避（探针间隔 ×1.5 递增）

令牌桶（可选，rate>0 时启用）：全局视频提交速率门控，防止容量刚释放被积压任务瞬时打满。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 状态常量
OPEN = "open"          # 上游饱和：长退避 + 视频任务不租约
CLOSED = "closed"      # 有容量：放行 + 短退避
PROBING = "probing"    # 冷却后探路：队头探针提前

# 优先级放行错峰：CLOSED 放行时按 priority 排序错峰提交，避免同刻风暴。
# 每个优先级档位错开 PRIORITY_RELEASE_STAGGER_SEC 秒。
PRIORITY_RELEASE_STAGGER_SEC = 2
RELEASE_MAX_STAGGER_SEC = 60


def _backend_dir() -> Path:
    try:
        from app.config import _BACKEND_DIR

        return Path(_BACKEND_DIR)
    except Exception:  # noqa: BLE001
        return Path(__file__).resolve().parents[2]


class CapacityRadar:
    """进程单例容量雷达：submit 结果驱动 + 文件快照恢复 + 令牌桶限速。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._status: str = OPEN  # 冷启动按饱和处理：先长退避、靠 probe 探路，避免启动即盲打
        self._last_success_at: float = 0.0  # monotonic
        self._probe_at: float = 0.0
        self._consec_fail: int = 0
        # 令牌桶（提交速率门控）
        self._bucket_tokens: float = 0.0
        self._bucket_last_refill: float = 0.0
        # wall-clock 时间戳（快照恢复用；_load_snapshot 可能覆盖）
        self._last_success_wall: float = 0.0
        self._probe_wall: float = 0.0
        # 探针头（canary）：OPEN/PROBING 状态下被调度器放行的任务，多车道多值。
        # task_id → lane（"paid" 付费快车道 / "free" 免费兜底）。
        # 饱和期付费任务并行多槽探针（默认 4），免费保留兜底 1 槽，
        # 订阅/余额用户不再与免费任务共用单一 30s 探针慢车道。
        self._probe_heads: dict[int, str] = {}
        # 刚从饱和态转入 CLOSED（供调度器消费一次 → 批量放行 + heal）
        self._just_closed = False
        self._snapshot_path: Path | None = None
        self._load_snapshot()

    # ---------- 快照（重启恢复，避免冷启动又盲打一轮） ----------

    def _default_snapshot_path(self) -> Path:
        return _backend_dir() / "data" / "capacity_radar.json"

    def set_snapshot_path(self, path: Path | str) -> None:
        with self._lock:
            self._snapshot_path = Path(path) if path else None

    def _load_snapshot(self) -> None:
        path = self._snapshot_path or self._default_snapshot_path()
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        self._status = str(data.get("status") or OPEN)
        self._consec_fail = int(data.get("consec_fail") or 0)
        # 快照里存的是 wall-clock（UTC）；恢复 last_success_at 相对当前 monotonic 无意义，
        # 但 closed 的 stale 判定需要绝对时间戳，故额外存 wall 时间戳。
        self._last_success_wall: float = float(data.get("last_success_wall") or 0.0)
        self._probe_wall: float = float(data.get("probe_wall") or 0.0)

    def _save_snapshot(self) -> None:
        path = self._snapshot_path or self._default_snapshot_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "status": self._status,
                "consec_fail": self._consec_fail,
                "last_success_wall": self._last_success_wall,
                "probe_wall": self._probe_wall,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            path.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            logger.debug("capacity radar snapshot save failed", exc_info=True)

    # ---------- 配置读取（带默认值，缺字段也能跑） ----------

    @staticmethod
    def _settings() -> Any:
        from app.config import get_settings

        return get_settings()

    def probe_interval_sec(self) -> float:
        s = self._settings()
        base = max(5.0, float(getattr(s, "video_capacity_probe_interval_sec", 30)))
        cap = max(base, float(getattr(s, "video_capacity_probe_max_interval_sec", 180)))
        # 连败 ×1.5 递增：probe_interval × 1.5^n（n=consec_fail），封顶
        grown = base * (1.5 ** min(max(self._consec_fail, 0), 8))
        return min(grown, cap)

    def stale_probe_min(self) -> float:
        return max(1.0, float(getattr(self._settings(), "video_capacity_stale_probe_min", 5)))

    # ---------- 状态驱动的退避参数（capacity_retry 用） ----------

    def open_backoff_sec(self) -> int:
        """OPEN（饱和）长退避：base × factor^n（n=连败次数），封顶。默认 120 → 180 → 270 → … → 600s。"""
        s = self._settings()
        base = max(30, int(getattr(s, "video_capacity_open_backoff_base_sec", 120)))
        factor = max(1.0, float(getattr(s, "video_capacity_open_backoff_factor", 1.5)))
        cap = max(base, int(getattr(s, "video_capacity_open_backoff_max_sec", 600)))
        with self._lock:
            n = max(0, self._consec_fail - 1)
        return int(min(base * (factor ** min(n, 6)), cap))

    def closed_backoff_sec(self) -> int:
        """CLOSED（有容量）短退避：容量刚释放后排队的 429 快速重来。"""
        s = self._settings()
        return max(1, int(getattr(s, "video_capacity_closed_backoff_sec", 3)))

    # ---------- 状态判定 ----------

    def status(self) -> str:
        with self._lock:
            return self._status

    def is_open(self) -> bool:
        return self.status() == OPEN

    def is_closed(self) -> bool:
        return self.status() == CLOSED

    def is_probing(self) -> bool:
        return self.status() == PROBING

    # ---------- 事件驱动（submit 结果） ----------

    def record_success(self) -> dict[str, Any]:
        """submit 命中上游容量并成功受理 → CLOSED。返回是否刚从饱和态转入 CLOSED（供批量放行）。"""
        with self._lock:
            now_wall = time.time()
            just_opened = self._status != CLOSED
            self._status = CLOSED
            self._consec_fail = 0
            self._last_success_wall = now_wall
            self._last_success_at = time.monotonic()
            self._just_closed = just_opened
            self._probe_heads = {}
            self._reset_bucket()
            self._save_snapshot()
        if just_opened:
            logger.info("capacity radar -> CLOSED (upstream has capacity)")
            self._heal_keys()
        return {"status": CLOSED, "just_opened": just_opened}

    def pop_just_closed(self) -> bool:
        """取走并清零 just_closed 标志（调度器每 tick 消费一次，触发批量放行）。"""
        with self._lock:
            flag = self._just_closed
            self._just_closed = False
            return flag

    # ---------- 探针头（canary）协调：调度器放行队头视频任务当探针 ----------

    def designate_probe_head(self, task_id: int, lane: str = "paid") -> None:
        """指定单个探针头（单值兼容 API；多槽场景用 designate_probe_heads）。"""
        with self._lock:
            self._probe_heads[int(task_id)] = str(lane or "paid")

    def designate_probe_heads(self, heads: list[tuple[int, str]]) -> None:
        """多车道探针头批量指定：[(task_id, lane), ...]，lane ∈ paid/free。

        付费快车道（并行多槽，饱和期每分钟 4 个提交窗口）+ 免费兜底 1 槽。
        整组替换旧探针头（探针时刻到点后重新指定）。
        """
        with self._lock:
            for task_id, lane in heads:
                self._probe_heads[int(task_id)] = str(lane or "paid")

    def get_probe_head(self) -> int | None:
        """最早（id 最小）的探针头；兼容旧单值调用点。无探针返回 None。"""
        with self._lock:
            if not self._probe_heads:
                return None
            return min(self._probe_heads)

    def get_probe_heads(self) -> dict[int, str]:
        """当前全部探针头：{task_id: lane}。"""
        with self._lock:
            return dict(self._probe_heads)

    def release_probe_head(self, task_id: int) -> bool:
        """capacity_retry 在 429 后调用：该任务是否为当前探针头（是则短退避、继续当队头探针）。"""
        with self._lock:
            return self._probe_heads.pop(int(task_id), None) is not None

    def clear_probe_heads(self) -> None:
        with self._lock:
            self._probe_heads = {}

    def record_capacity_fail(self) -> dict[str, Any]:
        """submit 命中 429/队列满 → OPEN。返回下次探针间隔（秒）。"""
        with self._lock:
            self._status = OPEN
            self._consec_fail += 1
            interval = self.probe_interval_sec()
            self._probe_wall = time.time() + interval
            self._reset_bucket()  # 429 回 OPEN 后清空令牌桶，避免积压补放
            self._save_snapshot()
        logger.info("capacity radar -> OPEN (429 saturated) probe_in=%.0fs consec_fail=%d", interval, self._consec_fail)
        return {"status": OPEN, "probe_interval_sec": interval}

    # ---------- 调度 tick 驱动的探针/放行决策 ----------

    def maybe_stale_probe(self, now_wall: float | None = None) -> bool:
        """CLOSED 且距上次成功超过 stale_probe_min → 转 PROBING（主动探路）。返回是否已进入探针节奏。"""
        now_wall = now_wall if now_wall is not None else time.time()
        with self._lock:
            if self._status != CLOSED:
                return False
            stale_sec = self.stale_probe_min() * 60.0
            if not self._last_success_wall or (now_wall - self._last_success_wall) < stale_sec:
                return False
            self._status = PROBING
            self._probe_wall = now_wall + self.probe_interval_sec()
            self._save_snapshot()
        logger.info("capacity radar -> PROBING (stale %ss, probe ahead)", int(stale_sec))
        return True

    def probe_head_due(self, now_wall: float | None = None) -> bool:
        """PROBING/OPEN 且到探针时刻 → 放行队头任务当探针。"""
        now_wall = now_wall if now_wall is not None else time.time()
        with self._lock:
            if self._status not in (PROBING, OPEN):
                return False
            # 到探针时刻才放行队头（OPEN 长退避期满；PROBING 到 probe_at）
            if self._probe_wall <= 0:
                return True
            return now_wall >= self._probe_wall

    def release_offset_sec(self, priority: int) -> int:
        """CLOSED 批量放行时按优先级错峰：高优先级（数值小）先提交。

        priority 越小越优先（调度器按 priority ASC 出队）。付费 100 / 免费 120 →
        付费先于免费，且同档位错开 STAGGER_SEC，避免同刻风暴。
        """
        p = max(0, int(priority or 100))
        return int(min((p // 10) * PRIORITY_RELEASE_STAGGER_SEC, RELEASE_MAX_STAGGER_SEC))

    # ---------- 出队准入门控（调度器用） ----------

    def should_admit_video(self, task_type: str, is_head_probe: bool) -> bool:
        """OPEN 时视频类任务不租约（省执行槽、不空转），仅队头 probe 任务放行。
        非视频任务或 CLOSED 放行；PROBING/OPEN 仅放行队头探针。"""
        st = self.status()
        if st == CLOSED:
            return True
        # PROBING / OPEN：只放行队头（is_head_probe），其余视频任务仍不租约
        return is_head_probe

    def video_retry_rounds(self) -> int:
        """按容量状态决定 submit 内部换 key 重试轮数：饱和期坍缩为 1（立即重排队，
        把决策权交给雷达 + 令牌桶），有容量/探针期保留多轮（此时换 key 才有意义）。"""
        s = self._settings()
        if not bool(getattr(s, "video_capacity_adaptive_enabled", True)):
            return int(getattr(s, "agnes_video_retry_rounds", 8))
        base = int(getattr(s, "agnes_video_retry_rounds", 8))
        if self.is_open():
            return 1  # 饱和：一次 429 立即重排队，往返从 ~11min 塌到 ~15s
        return max(1, min(base, 6))  # CLOSED/PROBING：多轮换 key 命中率高

    # ---------- 令牌桶（提交速率门控） ----------

    def _reset_bucket(self) -> None:
        rate = self._submit_rate()
        if rate > 0:
            # 容量事件后令牌桶清空（避免积压补放打满容量）
            self._bucket_tokens = 0.0
            self._bucket_last_refill = time.monotonic()

    def _submit_rate(self) -> float:
        return max(0.0, float(getattr(self._settings(), "video_capacity_submit_rate", 0.0)))

    def acquire_submit_slot(self) -> float:
        """取一个视频提交令牌；返回需要等待的秒数（0=可立即提交，inf 之外的值表示建议退避）。
        rate<=0（未启用限速）→ 恒 0。"""
        rate = self._submit_rate()
        if rate <= 0:
            return 0.0
        with self._lock:
            now = time.monotonic()
            if self._bucket_last_refill <= 0:
                self._bucket_last_refill = now
            elapsed = now - self._bucket_last_refill
            self._bucket_last_refill = now
            # 每秒补 rate 个令牌，桶容量 = rate（防突发）
            self._bucket_tokens = min(rate, self._bucket_tokens + elapsed * rate)
            if self._bucket_tokens >= 1.0:
                self._bucket_tokens -= 1.0
                return 0.0
            # 缺 1 个令牌需要多久攒够
            deficit = 1.0 - self._bucket_tokens
            return deficit / rate

    # ---------- key 池联动 ----------

    def _heal_keys(self) -> None:
        try:
            from app.services import agnes_key_pool

            result = agnes_key_pool.heal_exhausted()
            if result.get("count"):
                logger.info("capacity radar healed %d exhausted keys on CLOSED", result["count"])
        except Exception:  # noqa: BLE001
            logger.debug("capacity radar heal_keys failed", exc_info=True)


# 进程单例
_radar = CapacityRadar()


def get_capacity_radar() -> CapacityRadar:
    return _radar


def reset_capacity_radar() -> None:
    global _radar
    _radar = CapacityRadar()


# ---------- 管理端/调试用状态快照 ----------

def radar_status() -> dict[str, Any]:
    r = get_capacity_radar()
    with r._lock:
        st = r._status
        consec = r._consec_fail
        last_wall = r._last_success_wall
        probe_wall = r._probe_wall
    now_wall = time.time()
    return {
        "status": st,
        "consec_fail": consec,
        "probe_interval_sec": r.probe_interval_sec(),
        "last_success_ago_sec": (now_wall - last_wall) if last_wall else None,
        "probe_at_ago_sec": (probe_wall - now_wall) if probe_wall else None,
        "video_retry_rounds": r.video_retry_rounds(),
        "submit_rate": r._submit_rate(),
        "adaptive_enabled": bool(getattr(r._settings(), "video_capacity_adaptive_enabled", True)),
    }
