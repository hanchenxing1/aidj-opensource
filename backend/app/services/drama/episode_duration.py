"""每集时长 profile：把用户选的「每集时长」(1/2/5/10 分钟) 映射到正文字数与分镜预算。

时长档真正驱动下游生成：
- 正文目标字数 / 最短字数 / 场数提示（影响 LLM 分集正文长度）
- 分镜条数上限 / 整集时长预算（影响分镜规划与合并）

未提供或非法值时回退到默认 1 分钟档。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 支持的四档时长（秒）。默认 60（1 分钟）。
EPISODE_DURATION_PRESETS_SEC = (60, 120, 300, 600)
DEFAULT_EPISODE_DURATION_SEC = 60

# 单镜规划上限（秒）：跟随后台 seedance_duration_max，且不超 Seedance 单条收紧值 15，
# 使"分镜预算能容纳的镜数"与规划/提交的实际单镜时长一致（如后台 12s → 规划 12s）。
_SINGLE_SHOT_DESIGN_MAX_SEC = 15


def _single_shot_max_sec() -> int:
    try:
        from app.config import get_settings

        v = int(
            getattr(
                get_settings(),
                "seedance_duration_max",
                _SINGLE_SHOT_DESIGN_MAX_SEC,
            )
            or _SINGLE_SHOT_DESIGN_MAX_SEC
        )
    except Exception:  # noqa: BLE001
        v = _SINGLE_SHOT_DESIGN_MAX_SEC
    return max(4, min(v, _SINGLE_SHOT_DESIGN_MAX_SEC))


@dataclass(frozen=True)
class EpisodeDurationProfile:
    """一集的目标制作规格。"""

    minutes: int
    target_chars: int
    min_chars: int
    scene_hint: str
    frag_max: int
    duration_budget_sec: int

    @property
    def label(self) -> str:
        return f"{self.minutes} 分钟"


_PROFILES: dict[int, EpisodeDurationProfile] = {
    1: EpisodeDurationProfile(
        minutes=1,
        target_chars=550,
        min_chars=450,
        scene_hint="2-3 场",
        frag_max=5,
        duration_budget_sec=60,
    ),
    2: EpisodeDurationProfile(
        minutes=2,
        target_chars=1100,
        min_chars=900,
        scene_hint="4-5 场",
        frag_max=9,
        duration_budget_sec=120,
    ),
    5: EpisodeDurationProfile(
        minutes=5,
        target_chars=2600,
        min_chars=2000,
        scene_hint="8-10 场",
        frag_max=21,
        duration_budget_sec=300,
    ),
    10: EpisodeDurationProfile(
        minutes=10,
        target_chars=5000,
        min_chars=3800,
        scene_hint="14-18 场",
        frag_max=42,
        duration_budget_sec=600,
    ),
}

DEFAULT_PROFILE = _PROFILES[1]

# 分集正文/分镜的「镜数下限」：即便时长档很短也保证多镜，避免退化成 1 镜
MIN_FRAGMENTS_PER_EPISODE = 4


def _clamp_frag_max(budget_sec: int, profile: EpisodeDurationProfile) -> int:
    # 条数上限不超过预算能容纳的镜数 + 少量余量；同时保证至少 4 镜
    by_budget = max(1, budget_sec // _single_shot_max_sec())
    return max(MIN_FRAGMENTS_PER_EPISODE, by_budget, profile.frag_max)


def profile_from_seconds(seconds: Any) -> EpisodeDurationProfile:
    """把秒数映射到最接近的时长档。"""
    try:
        sec = int(seconds or 0)
    except (TypeError, ValueError):
        sec = 0
    if sec <= 0:
        return DEFAULT_PROFILE
    best = min(EPISODE_DURATION_PRESETS_SEC, key=lambda p: abs(p - sec))
    profile = _PROFILES[best // 60]
    profile = EpisodeDurationProfile(
        minutes=profile.minutes,
        target_chars=profile.target_chars,
        min_chars=profile.min_chars,
        scene_hint=profile.scene_hint,
        frag_max=_clamp_frag_max(sec, profile),
        duration_budget_sec=sec,
    )
    return profile


def resolve_episode_duration_profile(
    project_params: dict[str, Any] | None = None,
    script_params: dict[str, Any] | None = None,
) -> EpisodeDurationProfile:
    """从项目/剧本参数解析每集时长档；缺省回退默认 1 分钟。"""
    for raw in [
        (project_params or {}).get("episode_duration_sec"),
        (script_params or {}).get("episode_duration_sec"),
    ]:
        profile = profile_from_seconds(raw)
        if raw is not None:
            return profile
    return DEFAULT_PROFILE


def body_char_targets(profile: EpisodeDurationProfile | None = None) -> tuple[int, int, str]:
    """返回 (目标字数, 最短字数, 场数提示)。"""
    p = profile or DEFAULT_PROFILE
    return p.target_chars, p.min_chars, p.scene_hint


def fragment_budget_of(profile: EpisodeDurationProfile | None = None) -> tuple[int, int]:
    """返回 (分镜条数上限, 整集时长预算秒)。"""
    p = profile or DEFAULT_PROFILE
    return p.frag_max, p.duration_budget_sec
