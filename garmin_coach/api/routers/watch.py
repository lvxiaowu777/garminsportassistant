"""手表绑定路由：薄壳，规则全在 ``services/watch.py``。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends

from ..deps import require_user
from ..schemas import (
    WatchBindRequest,
    WatchMfaRequest,
    WatchSelectRequest,
    WatchToggleRequest,
)

from garmin_coach.services import watch as watch_service

router = APIRouter(prefix="/api/watch", tags=["watch"])


async def _run(fn, *args):
    """service 是同步函数（Garmin 登录会阻塞），放线程池跑，别卡事件循环。

    业务异常 ``AppError`` 由 ``server.py`` 的全局 handler 翻译成状态码。
    """
    return await asyncio.to_thread(fn, *args)


@router.post("/bind")
async def api_bind_watch(req: WatchBindRequest, user_id: int = Depends(require_user)):
    """绑定手表品牌。Garmin 走官方登录（动态判断 MFA）。"""
    return await _run(watch_service.bind, user_id, req.brand, req.account, req.password)


@router.post("/bind-mfa")
async def api_bind_watch_mfa(req: WatchMfaRequest, user_id: int = Depends(require_user)):
    """两步登录第二步：提交验证码完成绑定。"""
    return await _run(watch_service.bind_mfa, user_id, req.mfa_code)


@router.delete("/unbind")
async def api_unbind_watch(brand: str, user_id: int = Depends(require_user)):
    """解绑手表。"""
    return await _run(watch_service.unbind, user_id, brand)


@router.post("/toggle")
async def api_toggle_watch(req: WatchToggleRequest, user_id: int = Depends(require_user)):
    """开关设备：显示 / 关闭（关闭后同步跳过该品牌）。"""
    return await _run(watch_service.toggle, user_id, req.brand, req.enabled)


@router.get("/list")
async def api_watch_list(user_id: int = Depends(require_user)):
    """已绑定的手表列表。"""
    return watch_service.list_bindings(user_id)


@router.post("/select")
async def api_select_watch(req: WatchSelectRequest, user_id: int = Depends(require_user)):
    """单选设备：只启用一个品牌，其他关闭。"""
    return await _run(watch_service.select, user_id, req.brand)
