"""本地 Agnes key 池：只管理调用方主动写入的合法 key。

不做账号注册、邮箱收取、代理换 IP。数据落 backend/data/agnes_key_pool.json
（已 gitignore），活跃 key 可同步到 .env 的 AGNES_API_KEYS 供 AgnesClient 使用。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_STATUSES = ("active", "disabled", "exhausted")
_KEY_RE = re.compile(r"^sk-[A-Za-z0-9_\-]{8,}$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# 快车道：acquire(fast_lane=True) 时优先返回近 N 分钟内成功过的 key
FAST_LANE_WINDOW_SEC = 600


def _capacity_failure(error: str) -> bool:
    """判断失败文本是否为上游容量类（429/队列满）：容量是池的事不是 key 的事，
    不计 fail_count、不判 exhausted，避免饱和期误杀健康 key。"""
    text = (error or "").lower()
    if not text:
        return False
    markers = (
        "429",
        "fail_to_fetch_task",
        "video_queue_full",
        "video queue is full",
        "queue is full",
        "上游负载已饱和",
        "当前分组上游",
        "capacity limit",
        # distributor 503 无可用 channel：上游/配置故障而非 key 故障，
        # 计数 fail_count 会把饱和期全部健康 key 逐个误杀进 exhausted
        "model_not_found",
        "no available channel",
        "all channels failed",
    )
    return any(m in text for m in markers)


def pool_path() -> Path:
    from app.config import _BACKEND_DIR

    return _BACKEND_DIR / "data" / "agnes_key_pool.json"


def mask_key(key: str) -> str:
    key = (key or "").strip()
    if len(key) <= 10:
        return (key[:4] + "***") if key else ""
    return key[:7] + "…" + key[-4:]


def _empty_doc() -> dict[str, Any]:
    return {"version": 1, "updated_at": _utc_now(), "cursor": 0, "keys": []}


def _load_unlocked(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return _empty_doc()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_doc()
    if not isinstance(data, dict):
        return _empty_doc()
    keys = data.get("keys")
    if not isinstance(keys, list):
        data["keys"] = []
    data.setdefault("version", 1)
    data.setdefault("cursor", 0)
    data.setdefault("updated_at", _utc_now())
    return data


def _dump_unlocked(path: Path, data: dict[str, Any]) -> None:
    data["updated_at"] = _utc_now()
    raw = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="agnes_key_pool.", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _normalize_key(raw: str) -> str:
    return (raw or "").strip()


def _validate_key(key: str) -> str:
    key = _normalize_key(key)
    if not key:
        raise ValueError("key 为空")
    if not _KEY_RE.match(key):
        raise ValueError("key 格式无效（需要 sk- 开头的合法密钥）")
    return key


def _find(data: dict[str, Any], token: str) -> dict[str, Any] | None:
    token = _normalize_key(token)
    if not token:
        return None
    keys: list[dict[str, Any]] = data.get("keys") or []
    for row in keys:
        if row.get("key") == token:
            return row
    if len(token) >= 4:
        hits = [row for row in keys if str(row.get("key") or "").endswith(token)]
        if len(hits) == 1:
            return hits[0]
    rid = token.lower()
    for row in keys:
        if str(row.get("id") or "").lower() == rid:
            return row
    return None


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "masked": mask_key(str(row.get("key") or "")),
        "status": row.get("status") or "active",
        "note": row.get("note") or "",
        "added_at": row.get("added_at"),
        "last_used_at": row.get("last_used_at"),
        "last_success_at": row.get("last_success_at"),
        "last_error": row.get("last_error") or "",
        "fail_count": int(row.get("fail_count") or 0),
        "success_count": int(row.get("success_count") or 0),
    }


def load() -> dict[str, Any]:
    with _LOCK:
        return _load_unlocked(pool_path())


def list_keys(*, include_disabled: bool = True) -> list[dict[str, Any]]:
    data = load()
    out = []
    for row in data.get("keys") or []:
        if not include_disabled and (row.get("status") or "active") != "active":
            continue
        out.append(_public(row))
    return out


def stats() -> dict[str, Any]:
    data = load()
    rows: list[dict[str, Any]] = data.get("keys") or []
    counts = {name: 0 for name in _STATUSES}
    for row in rows:
        status = row.get("status") or "active"
        counts[status] = counts.get(status, 0) + 1
    return {
        "total": len(rows),
        "active": counts.get("active", 0),
        "disabled": counts.get("disabled", 0),
        "exhausted": counts.get("exhausted", 0),
        "updated_at": data.get("updated_at"),
        "path": str(pool_path()),
    }


def add_keys(keys: list[str], *, note: str = "") -> dict[str, Any]:
    """写入调用方提供的 key；重复则忽略。返回 added/skipped。"""
    cleaned: list[str] = []
    skipped: list[str] = []
    for raw in keys:
        key = _validate_key(raw)
        if key in cleaned:
            skipped.append(mask_key(key))
            continue
        cleaned.append(key)
    if not cleaned:
        raise ValueError("key 列表为空")
    note = (note or "").strip()[:120]
    added: list[str] = []
    with _LOCK:
        path = pool_path()
        data = _load_unlocked(path)
        existing = {str(row.get("key") or "") for row in data.get("keys") or []}
        for key in cleaned:
            if key in existing:
                skipped.append(mask_key(key))
                continue
            data["keys"].append(
                {
                    "id": "k_" + uuid.uuid4().hex[:10],
                    "key": key,
                    "status": "active",
                    "note": note,
                    "added_at": _utc_now(),
                    "last_used_at": None,
                    "last_success_at": None,
                    "last_error": "",
                    "fail_count": 0,
                    "success_count": 0,
                }
            )
            existing.add(key)
            added.append(mask_key(key))
        _dump_unlocked(path, data)
    return {"added": added, "skipped": skipped, "active": stats()["active"]}


def set_status(token: str, status: str) -> dict[str, Any]:
    status = (status or "").strip().lower()
    if status not in _STATUSES:
        raise ValueError("status 必须是 active / disabled / exhausted")
    with _LOCK:
        path = pool_path()
        data = _load_unlocked(path)
        row = _find(data, token)
        if row is None:
            raise KeyError("key 不存在")
        row["status"] = status
        if status == "active":
            row["last_error"] = ""
        _dump_unlocked(path, data)
        return _public(row)


def remove_key(token: str) -> dict[str, Any]:
    with _LOCK:
        path = pool_path()
        data = _load_unlocked(path)
        row = _find(data, token)
        if row is None:
            raise KeyError("key 不存在")
        data["keys"] = [item for item in data["keys"] if item is not row]
        _dump_unlocked(path, data)
        return _public(row)


def acquire(
    *,
    exclude: set[str] | frozenset[str] | None = None,
    fast_lane: bool = False,
) -> dict[str, Any]:
    """轮询取一把 active key；无可用时抛出 LookupError。

    fast_lane=True（快车道）：优先返回近 FAST_LANE_WINDOW_SEC 内成功过且不在
    exclude 中的 key（实测 28 把 key 仅约 1/4 可 200，选中近期成功 key 命中率最高）；
    无近期成功记录时回落普通轮转。
    """
    blocked = {str(x).strip() for x in (exclude or set()) if x}
    with _LOCK:
        path = pool_path()
        data = _load_unlocked(path)
        rows: list[dict[str, Any]] = data.get("keys") or []
        active = [
            row
            for row in rows
            if (row.get("status") or "active") == "active"
            and str(row.get("key") or "") not in blocked
        ]
        if not active:
            raise LookupError("没有可用的 active key")
        pick: dict[str, Any] | None = None
        if fast_lane:
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=FAST_LANE_WINDOW_SEC)
            for row in active:
                ls = row.get("last_success_at")
                if not ls:
                    continue
                try:
                    ts = datetime.fromisoformat(str(ls))
                except ValueError:
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts >= cutoff:
                    pick = row
                    break
        if pick is None:
            cursor = int(data.get("cursor") or 0) % len(active)
            pick = active[cursor]
            data["cursor"] = (cursor + 1) % len(active)
        row = pick
        row["last_used_at"] = _utc_now()
        _dump_unlocked(path, data)
        pub = _public(row)
        pub["key"] = row["key"]
        return pub


def mark_result(token: str, *, ok: bool, error: str = "") -> dict[str, Any]:
    with _LOCK:
        path = pool_path()
        data = _load_unlocked(path)
        row = _find(data, token)
        if row is None:
            raise KeyError("key 不存在")
        if ok:
            row["success_count"] = int(row.get("success_count") or 0) + 1
            row["last_error"] = ""
            row["last_success_at"] = _utc_now()
            # 成功即证明 key 健康：清 fail_count，并从 exhausted 自动恢复
            if int(row.get("fail_count") or 0) > 0:
                row["fail_count"] = 0
            if (row.get("status") or "active") == "exhausted":
                row["status"] = "active"
        else:
            # 容量类失败（429/队列满）是池的事不是 key 的事：只留痕不计数、不判 exhausted，
            # 避免饱和期把健康 key 误杀（fail_count≥5→exhausted）导致池越缩越小。
            if _capacity_failure(error):
                row["last_error"] = (error or "").strip()[:200]
            else:
                row["fail_count"] = int(row.get("fail_count") or 0) + 1
                row["last_error"] = (error or "").strip()[:200]
                if row["fail_count"] >= 5:
                    row["status"] = "exhausted"
        _dump_unlocked(path, data)
        return _public(row)


def heal_exhausted() -> dict[str, Any]:
    """批量把 exhausted 的 key 恢复 active（清零 fail_count）。

    由容量雷达在 CLOSED（有容量）迁移时自动调用：容量恢复后健康 key 应立即
    回到快车道，而不是等 5 次成功才自愈。也供管理端手动触发。
    """
    with _LOCK:
        path = pool_path()
        data = _load_unlocked(path)
        rows: list[dict[str, Any]] = data.get("keys") or []
        healed: list[str] = []
        for row in rows:
            if (row.get("status") or "active") == "exhausted":
                row["status"] = "active"
                row["fail_count"] = 0
                row["last_error"] = ""
                healed.append(_public(row)["masked"])
        if healed:
            _dump_unlocked(path, data)
        return {"healed": healed, "count": len(healed)}


def active_secrets() -> list[str]:
    data = load()
    return [
        str(row.get("key") or "")
        for row in data.get("keys") or []
        if (row.get("status") or "active") == "active" and row.get("key")
    ]


def import_from_env() -> dict[str, Any]:
    """把当前 .env / 进程环境里的 AGNES_API_KEYS 并入池（去重）。"""
    from app.config import get_settings, reload_settings

    reload_settings()
    settings = get_settings()
    keys = [k.strip() for k in (settings.agnes_api_keys or "").split(",") if k.strip()]
    if not keys and (settings.agnes_api_key or "").strip():
        keys = [settings.agnes_api_key.strip()]
    if not keys:
        return {"added": [], "skipped": [], "active": stats()["active"], "imported": 0}
    result = add_keys(keys, note="import-env")
    result["imported"] = len(keys)
    return result


def sync_to_env() -> dict[str, Any]:
    """把池内 active key 写回 .env AGNES_API_KEYS 并热更新客户端。"""
    from app.config import _ENV_FILE, reload_settings
    from app.services import agnes_client

    keys = active_secrets()
    text_env = _ENV_FILE.read_text(encoding="utf-8") if _ENV_FILE.is_file() else ""
    lines = text_env.splitlines()
    idx = [i for i, ln in enumerate(lines) if ln.startswith("AGNES_API_KEYS=")]
    value = "AGNES_API_KEYS=" + ",".join(keys)
    if idx:
        lines[idx[0]] = value
    else:
        lines.append(value)
    payload = "\n".join(lines)
    if payload and not payload.endswith("\n"):
        payload += "\n"
    _ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    _ENV_FILE.write_text(payload, encoding="utf-8")
    reload_settings()
    agnes_client.reset_agnes_client()
    return {"ok": True, "written": len(keys), "env_file": str(_ENV_FILE)}
