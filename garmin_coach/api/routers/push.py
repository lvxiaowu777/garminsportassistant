"""推送：设备 token 注册 / 状态 / 测试 / 手动触发提醒检查。

Firebase 没配时这些接口仍然返回 200（`firebase_ready: false`），不影响其他功能。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..schemas import PushRegisterRequest, PushTestRequest

from garmin_coach.integrations.push import (
    check_and_notify,
    get_active_tokens,
    init_firebase,
    register_device,
    send_push,
)

router = APIRouter(prefix="/api/push", tags=["push"])


@router.post("/register")
async def api_push_register(req: PushRegisterRequest):
    """注册设备推送 Token。Android App 启动时调用。"""
    if not req.token.strip():
        raise HTTPException(400, "Token 不能为空")
    ok = register_device(req.token.strip(), req.platform)
    return {"ok": ok, "token": req.token[:20] + "..."}


@router.get("/status")
async def api_push_status():
    """推送状态：Firebase 是否就绪 + 已注册设备数。"""
    return {
        "firebase_ready": init_firebase(),
        "active_devices": len(get_active_tokens()),
    }


@router.post("/test")
async def api_push_test(req: PushTestRequest):
    """测试推送：向所有活跃设备发一条通知。"""
    tokens = get_active_tokens()
    if not tokens:
        return {"ok": False, "message": "无活跃设备"}
    sent = 0
    for token in tokens:
        if send_push(token, req.title, req.body, req.click_action):
            sent += 1
    return {"ok": True, "sent": sent, "total": len(tokens)}


@router.post("/check")
async def api_push_check():
    """手动触发训练提醒检查（调试用）。"""
    return check_and_notify()
