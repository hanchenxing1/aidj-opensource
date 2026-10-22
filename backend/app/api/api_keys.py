from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import get_current_user
from app.models import User
from app.schemas_api import (
    AgnesKeyOut,
    AgnesRefreshOut,
    ApiKeyCreateRequest,
    ApiKeyCreatedOut,
    ApiKeyOut,
)
from app.services import agnes_vip_link, api_keys

router = APIRouter(prefix="/user/api-keys", tags=["api-keys"])


@router.get("", response_model=list[ApiKeyOut])
async def list_keys(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ApiKeyOut]:
    rows = await api_keys.list_api_keys(db, user.id)
    # 列表只展示 pf_live_ 鉴权密钥；Agnes 免费 key 走 /agnes 端点单独展示
    rows = [r for r in rows if r.source != "agnes"]
    return [ApiKeyOut.model_validate(row) for row in rows]


@router.post("", response_model=ApiKeyCreatedOut)
async def create_key(
    body: ApiKeyCreateRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ApiKeyCreatedOut:
    row, raw = await api_keys.create_api_key(db, user, body.name)
    await db.commit()
    await db.refresh(row)
    out = ApiKeyOut.model_validate(row)
    return ApiKeyCreatedOut(**out.model_dump(), secret=raw)


@router.delete("/{key_id}")
async def revoke_key(
    key_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    ok = await api_keys.revoke_api_key(db, user.id, key_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Key 不存在或已撤销")
    await db.commit()
    return {"ok": True}


@router.get("/agnes", response_model=AgnesKeyOut)
async def get_agnes_key(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """查询当前用户已绑定的 Agnes 免费 key（dj 站点 cloud_vip 直读）。"""
    row = await api_keys.get_agnes_api_key(db, user.id)
    return {
        "bound": bool(row),
        "key": (row.plain_key or None) if row else None,
        "sync_available": agnes_vip_link.is_available(),
    }


@router.post("/agnes/refresh", response_model=AgnesRefreshOut)
async def refresh_agnes_key(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """按用户邮箱重读 dj 站点 cloud_vip，刷新/首次绑定 Agnes 免费 key。"""
    row_before = await api_keys.get_agnes_api_key(db, user.id)
    raw = agnes_vip_link.get_agnes_key_for_email(user.email)
    if not raw or not api_keys.is_plausible_agnes_key(raw):
        return {
            "bound": bool(row_before),
            "key": (row_before.plain_key or None) if row_before else None,
            "sync_available": agnes_vip_link.is_available(),
            "ok": False,
            "message": "该邮箱尚未在 dj 站点开通 Agnes key，或 key 尚未生成，请稍后再试",
        }
    bound = await api_keys.bind_agnes_key(db, user, raw)
    await db.commit()
    return {
        "bound": bool(bound),
        "key": raw if bound else None,
        "sync_available": True,
        "ok": True,
        "message": "已同步 Agnes 免费 Key",
    }


@router.post("/agnes/unbind", response_model=AgnesKeyOut)
async def unbind_agnes_key_route(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """解绑当前用户的 Agnes 免费 key（dj 站点仍保留原 key，可随时再同步）。"""
    await api_keys.unbind_agnes_key(db, user.id)
    await db.commit()
    row = await api_keys.get_agnes_api_key(db, user.id)
    return {
        "bound": bool(row),
        "key": (row.plain_key or None) if row else None,
        "sync_available": agnes_vip_link.is_available(),
    }
