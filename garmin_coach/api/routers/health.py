"""健康数据：仪表盘 / 单日 / 区间 / 安全档位 / 活动 / 天气 / 同步 / 手动记录。"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException

from ..deps import SYNCERS, get_current_user
from ..schemas import RecordTrainingRequest, UpdateExercisesRequest

from garmin_coach.db.database import (
    delete_activity,
    get_activity,
    get_dashboard,
    get_day,
    get_last_sync_date,
    get_recent,
    get_watch_bindings,
    get_weather,
    record_training,
    update_activity_exercises,
    update_last_sync_date,
)
from garmin_coach.services.clock import period_of
from garmin_coach.services.safety import compute_safety

router = APIRouter(prefix="/api/health", tags=["health"])


@router.get("/dashboard")
async def api_dashboard(user_id: int = Depends(get_current_user)):
    """仪表盘聚合数据。"""
    dashboard = get_dashboard(user_id)
    dashboard["safety"] = compute_safety(user_id)

    now = dt.datetime.now()
    dashboard["datetime"] = now.strftime("%Y-%m-%d %H:%M")
    dashboard["period"] = period_of(now.hour)
    return dashboard


@router.get("/day/{date}")
async def api_day(date: str, user_id: int = Depends(get_current_user)):
    """某天详情。"""
    data = get_day(date, user_id)
    if not data:
        raise HTTPException(404, f"无 {date} 的数据")
    return data


@router.get("/recent/{days}")
async def api_recent(days: int = 7, user_id: int = Depends(get_current_user)):
    """最近 N 天。"""
    return get_recent(days, user_id)


@router.get("/safety")
async def api_safety(user_id: int = Depends(get_current_user)):
    """当前安全档位。"""
    return compute_safety(user_id)


@router.get("/activity/{activity_id}")
async def api_activity(activity_id: str, user_id: int = Depends(get_current_user)):
    """单次活动详情。"""
    data = get_activity(activity_id, user_id)
    if not data:
        raise HTTPException(404, f"无活动 {activity_id}")
    return data


@router.delete("/activity/{activity_id}")
async def api_delete_activity(activity_id: str, user_id: int = Depends(get_current_user)):
    """删除一条训练记录。"""
    ok = delete_activity(activity_id, user_id)
    if not ok:
        raise HTTPException(404, "记录不存在")
    return {"ok": True}


@router.put("/activity/{activity_id}")
async def api_update_activity(
    activity_id: str,
    req: UpdateExercisesRequest,
    user_id: int = Depends(get_current_user),
):
    """编辑力量训练的动作明细（删旧插新）。"""
    ok = update_activity_exercises(activity_id, user_id, req.exercises)
    if not ok:
        raise HTTPException(404, "记录不存在")
    return {"ok": True}


@router.get("/weather")
async def api_weather(user_id: int = Depends(get_current_user)):
    """今日天气。"""
    today = dt.date.today().isoformat()
    data = get_weather(today, user_id)
    if not data:
        raise HTTPException(404, "今日天气未同步")
    return data


@router.post("/sync")
async def api_sync_health(user_id: int = Depends(get_current_user)):
    """手动同步：当天首次全量 7 天，之后只同步当天。

    按品牌分发到 `deps.SYNCERS`；绑了但还没同步实现的品牌（apple 等）只回一条 note。
    """
    if user_id == 0:
        raise HTTPException(401, "请先登录")
    today = dt.date.today().isoformat()
    days = 1 if get_last_sync_date(user_id) == today else 7
    results: dict[str, dict] = {}
    brands = [b["brand"] for b in get_watch_bindings(user_id) if b.get("enabled")]
    for brand in brands:
        sync_fn = SYNCERS.get(brand)
        if sync_fn is None:
            results[brand] = {"days": 0, "error": None, "note": "该品牌暂不支持自动同步"}
            continue
        n, err = sync_fn(user_id, days=days)
        results[brand] = {"days": n, "error": err}
    update_last_sync_date(user_id, today)

    total = sum(v["days"] for v in results.values())
    errs = [v["error"] for v in results.values() if v["error"]]
    if errs and total == 0:
        raise HTTPException(400, errs[0])
    return {"ok": True, "days": total, "results": results}


@router.post("/sync-weather")
async def api_sync_weather(
    user_id: int = Depends(get_current_user),
    lon: float = 0.0,
    lat: float = 0.0,
):
    """手动触发天气同步（和风，按经纬度）。lon/lat 为 0 时用默认坐标。"""
    from garmin_coach.integrations.weather import sync_weather_to_db

    if not lon or not lat:
        lon, lat = 112.14, 32.04  # 默认襄阳
    try:
        today = dt.date.today().isoformat()
        weather = sync_weather_to_db(user_id, today, lon=lon, lat=lat)
        if weather is None:
            return {"ok": False, "message": "未定位"}
        return {"ok": True, "message": f"天气已同步 {today}", "location": weather.get("location")}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@router.post("/record-training")
async def api_record_training(
    req: RecordTrainingRequest,
    user_id: int = Depends(get_current_user),
):
    """手动记录一次训练（通过对话或手动输入）。"""
    ok = record_training(user_id, req.type, req.duration_min, req.notes, req.date)
    if ok:
        return {
            "ok": True,
            "message": f"已记录 {req.date or dt.date.today().isoformat()} {req.type}",
        }
    return {"ok": False, "message": "记录失败"}
