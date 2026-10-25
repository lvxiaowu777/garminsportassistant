"""手表绑定：绑定（含 Garmin MFA）/ 解绑 / 开关 / 列表 / 单选。

多品牌策略见 `deps.SUPPORTED_BRANDS` 与 `deps.SYNCERS`。
"""
from __future__ import annotations

import threading

from fastapi import APIRouter, Depends, HTTPException

from ..deps import SUPPORTED_BRANDS, get_current_user
from ..schemas import (
    WatchBindRequest,
    WatchMfaRequest,
    WatchSelectRequest,
    WatchToggleRequest,
)

from garmin_coach.db.database import (
    bind_watch,
    get_watch_bindings,
    select_watch,
    set_watch_enabled,
    unbind_watch,
)
from garmin_coach.integrations.garmin_sync import complete_mfa, login_and_store, sync_user_garmin

router = APIRouter(prefix="/api/watch", tags=["watch"])


@router.post("/bind")
async def api_bind_watch(req: WatchBindRequest, user_id: int = Depends(get_current_user)):
    """绑定手表品牌（需登录）。Garmin 走官方登录（动态判断 MFA）。"""
    if user_id == 0:
        raise HTTPException(401, "请先登录")
    if req.brand not in SUPPORTED_BRANDS:
        raise HTTPException(400, f"不支持的手表品牌: {req.brand}")

    # Garmin：用账号密码登录官方，验证凭据 + 动态判断是否需要两步验证
    if req.brand == "garmin":
        if not req.account or not req.password:
            raise HTTPException(400, "Garmin 需要邮箱和密码")
        needs_mfa, err = login_and_store(user_id, req.account, req.password)
        if err:
            raise HTTPException(400, f"登录失败: {err}")
        if needs_mfa:
            return {"ok": True, "needs_mfa": True, "message": "需要两步验证码"}
        # 登录成功：记录绑定 + 后台触发首次同步
        bind_watch(user_id, req.brand, req.account, auth_token="")
        threading.Thread(target=sync_user_garmin, args=(user_id, 7), daemon=True).start()
        return {"ok": True, "needs_mfa": False, "message": f"已绑定 {req.brand}"}

    # 其他品牌：暂只记录账号（多品牌预留：绑了就能出现在设备列表里，同步器还没实现）
    ok = bind_watch(user_id, req.brand, req.account, auth_token=req.password)
    if not ok:
        raise HTTPException(500, "绑定失败")
    return {"ok": True, "needs_mfa": False, "message": f"已绑定 {req.brand}"}


@router.post("/bind-mfa")
async def api_bind_watch_mfa(req: WatchMfaRequest, user_id: int = Depends(get_current_user)):
    """两步登录第二步：提交验证码完成 Garmin 绑定。"""
    if user_id == 0:
        raise HTTPException(401, "请先登录")
    if not req.mfa_code.strip():
        raise HTTPException(400, "验证码不能为空")
    ok, err, account = complete_mfa(user_id, req.mfa_code.strip())
    if not ok:
        raise HTTPException(400, err)
    # 记录绑定 + 后台触发首次同步
    if account:
        bind_watch(user_id, "garmin", account, auth_token="")
    threading.Thread(target=sync_user_garmin, args=(user_id, 7), daemon=True).start()
    return {"ok": True, "needs_mfa": False, "message": "已绑定 Garmin"}


@router.delete("/unbind")
async def api_unbind_watch(brand: str, user_id: int = Depends(get_current_user)):
    """解绑手表。"""
    if user_id == 0:
        raise HTTPException(401, "请先登录")
    ok = unbind_watch(user_id, brand)
    if not ok:
        raise HTTPException(404, f"未找到 {brand} 绑定")
    return {"ok": True, "message": f"已解绑 {brand}"}


@router.post("/toggle")
async def api_toggle_watch(req: WatchToggleRequest, user_id: int = Depends(get_current_user)):
    """开关设备：显示 / 关闭（不显示数据）。"""
    if user_id == 0:
        raise HTTPException(401, "请先登录")
    ok = set_watch_enabled(user_id, req.brand, req.enabled)
    if not ok:
        raise HTTPException(404, f"未找到 {req.brand} 绑定")
    return {
        "ok": True,
        "enabled": req.enabled,
        "message": f"{req.brand} 已{'显示' if req.enabled else '关闭'}",
    }


@router.get("/list")
async def api_watch_list(user_id: int = Depends(get_current_user)):
    """已绑定的手表列表。"""
    if user_id == 0:
        return []
    return get_watch_bindings(user_id)


@router.post("/select")
async def api_select_watch(req: WatchSelectRequest, user_id: int = Depends(get_current_user)):
    """单选设备：只启用一个品牌，其他关闭。"""
    if user_id == 0:
        raise HTTPException(401, "请先登录")
    ok = select_watch(user_id, req.brand)
    if not ok:
        raise HTTPException(404, f"未找到 {req.brand} 绑定")
    return {"ok": True, "active_brand": req.brand}
