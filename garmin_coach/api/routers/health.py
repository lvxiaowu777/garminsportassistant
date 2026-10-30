"""健康数据：仪表盘 / 单日 / 区间 / 安全档位 / 活动 / 天气 / 同步 / 手动记录。"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException

from ..deps import require_user
from ..schemas import RecordTrainingRequest, UpdateExercisesRequest

from garmin_coach.db.database import (
    delete_activity,
    get_activity,
    get_dashboard,
    get_day,
    get_recent,
    get_weather,
    update_activity_exercises,
)
from garmin_coach.services.clock import period_of
from garmin_coach.services.health_sync import sync_all_brands
from garmin_coach.services.safety import compute_safety
from garmin_coach.services.training import record_strength

router = APIRouter(prefix="/api/health", tags=["health"])


@router.get("/dashboard")
async def api_dashboard(user_id: int = Depends(require_user)):
    """仪表盘聚合数据。"""
    dashboard = get_dashboard(user_id)
    dashboard["safety"] = compute_safety(user_id)

    now = dt.datetime.now()
    dashboard["datetime"] = now.strftime("%Y-%m-%d %H:%M")
    dashboard["period"] = period_of(now.hour)
    return dashboard


@router.get("/day/{date}")
async def api_day(date: str, user_id: int = Depends(require_user)):
    """某天详情。"""
    data = get_day(date, user_id)
    if not data:
        raise HTTPException(404, f"无 {date} 的数据")
    return data


@router.get("/recent/{days}")
async def api_recent(days: int = 7, user_id: int = Depends(require_user)):
    """最近 N 天。"""
    return get_recent(days, user_id)


@router.get("/safety")
async def api_safety(user_id: int = Depends(require_user)):
    """当前安全档位。"""
    return compute_safety(user_id)


@router.get("/activity/{activity_id}")
async def api_activity(activity_id: str, user_id: int = Depends(require_user)):
    """单次活动详情。"""
    data = get_activity(activity_id, user_id)
    if not data:
        raise HTTPException(404, f"无活动 {activity_id}")
    return data


@router.delete("/activity/{activity_id}")
async def api_delete_activity(activity_id: str, user_id: int = Depends(require_user)):
    """删除一条训练记录。"""
    ok = delete_activity(activity_id, user_id)
    if not ok:
        raise HTTPException(404, "记录不存在")
    return {"ok": True}


@router.put("/activity/{activity_id}")
async def api_update_activity(
    activity_id: str,
    req: UpdateExercisesRequest,
    user_id: int = Depends(require_user),
):
    """编辑力量训练的动作明细（删旧插新）。"""
    ok = update_activity_exercises(activity_id, user_id, req.exercises)
    if not ok:
        raise HTTPException(404, "记录不存在")
    return {"ok": True}


@router.get("/weather")
async def api_weather(user_id: int = Depends(require_user)):
    """今日天气。"""
    today = dt.date.today().isoformat()
    data = get_weather(today, user_id)
    if not data:
        raise HTTPException(404, "今日天气未同步")
    return data


@router.post("/sync")
async def api_sync_health(user_id: int = Depends(require_user)):
    """手动同步：当天首次拉 7 天，之后只拉当天（规则在 services/health_sync）。"""
    import asyncio

    result = await asyncio.to_thread(sync_all_brands, user_id)
    return {"ok": True, **result}


@router.post("/sync-weather")
async def api_sync_weather(
    user_id: int = Depends(require_user),
    lon: float = 0.0,
    lat: float = 0.0,
):
    """手动触发天气同步（和风，按经纬度）。lon/lat 为 0 时用默认坐标。"""
    from garmin_coach.integrations.weather import sync_weather_to_db

    from garmin_coach.settings import settings

    if not lon or not lat:
        lon, lat = settings.default_weather_lon, settings.default_weather_lat
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
    user_id: int = Depends(require_user),
):
    """手动记录一次力量训练（幂等；跑步以手表为准）。"""
    import asyncio

    result = await asyncio.to_thread(
        record_strength,
        user_id,
        req.type,
        req.duration_min,
        req.notes,
        req.date,
        [e.model_dump(exclude_none=True) for e in req.exercises],
    )
    if result.get("ok"):
        detail = ""
        if result.get("merged"):
            detail = f"，合并动作：{'、'.join(result['merged'])}"
        return {
            "ok": True,
            "message": f"已记录 {req.date or dt.date.today().isoformat()} {req.type}{detail}",
            "created": result.get("created", False),
            "merged": result.get("merged", []),
            "added": result.get("added", []),
        }
    return {"ok": False, "message": "记录失败"}
