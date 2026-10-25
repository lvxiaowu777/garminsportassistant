"""认证：注册 / 登录 / 当前用户。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..deps import get_current_user  # noqa: F401  同时完成 sys.path 设置
from ..schemas import LoginRequest, RegisterRequest

from auth import create_token, hash_password, verify_password
from garmin_coach.db.database import create_user, get_user_by_phone, get_user_profile

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/register")
async def api_register(req: RegisterRequest):
    """手机号注册。"""
    phone = req.phone.strip()
    if not phone or len(phone) < 11:
        raise HTTPException(400, "请输入正确的手机号")
    if len(req.password) < 6:
        raise HTTPException(400, "密码至少 6 位")

    existing = get_user_by_phone(phone)
    if existing:
        raise HTTPException(409, "该手机号已注册，请直接登录")

    pw_hash = hash_password(req.password)
    user_id = create_user(phone, pw_hash)
    if user_id is None:
        raise HTTPException(500, "注册失败，请重试")

    token = create_token(user_id)
    return {"ok": True, "user_id": user_id, "token": token}


@router.post("/login")
async def api_login(req: LoginRequest):
    """手机号登录。"""
    phone = req.phone.strip()
    user = get_user_by_phone(phone)
    if not user:
        raise HTTPException(401, "该手机号未注册")

    if not verify_password(req.password, user["password_hash"]):
        raise HTTPException(401, "密码错误")

    token = create_token(user["id"])
    return {"ok": True, "user_id": user["id"], "token": token}


@router.get("/me")
async def api_me(user_id: int = Depends(get_current_user)):
    """返回当前登录用户的基础信息 + 是否完成过 onboarding。

    2026-08-20：去掉了 phone/created_at/nickname 字段（账号页面不再展示），
    新增 profile_completed 标志（用 goal IS NOT NULL 判断）+ training_types + races。
    """
    if user_id == 0:
        raise HTTPException(401, "请先登录")
    profile = get_user_profile(user_id)
    if not profile:
        raise HTTPException(404, "用户不存在")
    return {
        "user_id": user_id,
        "profile_completed": profile.get("goal") is not None,
        "height_cm": profile.get("height_cm"),
        "weight_kg": profile.get("weight_kg"),
        "age": profile.get("age"),
        "gender": profile.get("gender"),
        "body_fat_pct": profile.get("body_fat_pct"),
        "muscle_mass_kg": profile.get("muscle_mass_kg"),
        "goal": profile.get("goal"),
        "goal_target": profile.get("goal_target"),
        "training_years": profile.get("training_years"),
        "training_freq_per_week": profile.get("training_freq_per_week"),
        "training_types": profile.get("training_types") or [],
        "races": profile.get("races") or [],
    }
