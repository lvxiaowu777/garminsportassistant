"""用户档案：读取 / 更新。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..deps import get_current_user
from ..schemas import UpdateProfileRequest

from database import get_user_profile, update_user_profile

router = APIRouter(prefix="/api/user", tags=["user"])


@router.get("/profile")
async def api_get_profile(user_id: int = Depends(get_current_user)):
    """获取用户档案（需求8字段）。"""
    return get_user_profile(user_id)


@router.post("/profile")
async def api_update_profile(
    req: UpdateProfileRequest,
    user_id: int = Depends(get_current_user),
):
    """更新用户档案（需求8字段）。"""
    ok = update_user_profile(user_id, req.model_dump())
    return {"ok": ok}
