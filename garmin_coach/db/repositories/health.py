"""health_daily：单日/区间/仪表盘聚合；标准动作名。"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Optional

from sqlalchemy import delete, func, inspect, select, text, update

from garmin_coach.db.models import (
    Activity,
    AgentRun,
    AgentStep,
    ChatHistory,
    DailyPlan,
    DeviceToken,
    ExerciseName,
    HealthDaily,
    MemoryOutbox,
    StrengthSet,
    User,
    UserMemory,
    WatchBinding,
)

from ._base import (
    SessionLocal,
    _insert_stmt,
    _upsert,
    _upsert_append,
)

from .activities import _activity_row, _get_activities, _upsert_activity
from .watches import get_active_brand

#: 标准动作名（力量训练归一化）：(标准名, 别名, 部位)
DEFAULT_EXERCISES = [
    ("二头弯举", "举哑铃,哑铃弯举,杠铃弯举,弯举", "手臂"),
    ("三头下压", "三头,绳索下压,臂屈伸", "手臂"),
    ("肩推", "推肩,哑铃推举,杠铃推举,过头推举", "肩"),
    ("侧平举", "侧举,飞鸟", "肩"),
    ("前平举", "前举", "肩"),
    ("卧推", "平板卧推,杠铃卧推,哑铃卧推", "胸"),
    ("俯卧撑", "", "胸"),
    ("上斜卧推", "上斜推", "胸"),
    ("飞鸟", "哑铃飞鸟,夹胸", "胸"),
    ("引体向上", "引体,拉单杠", "背"),
    ("划船", "俯身划船,坐姿划船,杠铃划船", "背"),
    ("高位下拉", "下拉,宽握下拉", "背"),
    ("深蹲", "蹲,杠铃深蹲,哑铃深蹲,徒手深蹲", "腿"),
    ("硬拉", "罗马尼亚硬拉", "腿"),
    ("箭步蹲", "弓步蹲,弓箭步,箭步", "腿"),
    ("腿举", "倒蹬", "腿"),
    ("提踵", "踮脚", "腿"),
    ("卷腹", "仰卧起坐,腹肌,练腹", "核心"),
    ("平板支撑", "平板,核心支撑", "核心"),
    ("俄罗斯转体", "转体,俄式转体", "核心"),
]


def upsert_health_daily(data: dict, user_id: int = 0, brand: str = "garmin"):
    """插入或更新一天的完整健康数据。data 格式对齐 fetch_day 的嵌套输出。"""
    sleep = data.get("sleep") or {}
    recovery = data.get("recovery") or {}
    daily = data.get("daily") or {}
    fitness = data.get("fitness") or {}
    weather = data.get("weather") or {}
    race = fitness.get("race_predictions") or {}
    age = fitness.get("age") or {}

    cols = {
        "date": data["date"],
        "user_id": user_id,
        "brand": brand,
        "weekday": data.get("weekday"),
        "sleep_hours": sleep.get("hours"),
        "sleep_score": sleep.get("score"),
        "sleep_deep_min": sleep.get("deep_min"),
        "sleep_rem_min": sleep.get("rem_min"),
        "sleep_light_min": sleep.get("light_min"),
        "sleep_feedback": sleep.get("feedback"),
        "resting_hr": recovery.get("resting_hr"),
        "resting_hr_7d_avg": recovery.get("resting_hr_7d_avg"),
        "hrv": recovery.get("hrv"),
        "hrv_status": recovery.get("hrv_status"),
        "hrv_baseline_low": recovery.get("hrv_baseline_low"),
        "hrv_baseline_high": recovery.get("hrv_baseline_high"),
        "body_battery_high": recovery.get("body_battery_high"),
        "body_battery_low": recovery.get("body_battery_low"),
        "body_battery_wake": recovery.get("body_battery_wake"),
        "body_battery_charged": recovery.get("body_battery_charged"),
        "body_battery_now": recovery.get("body_battery_now"),
        "stress_avg": recovery.get("stress_avg"),
        "stress_qualifier": recovery.get("stress_qualifier"),
        "readiness": recovery.get("readiness"),
        "readiness_level": recovery.get("readiness_level"),
        "steps": daily.get("steps"),
        "kcal": daily.get("kcal"),
        "floors": daily.get("floors"),
        "moderate_min": daily.get("moderate_min"),
        "vigorous_min": daily.get("vigorous_min"),
        "training_status": fitness.get("training_status"),
        "training_feedback": fitness.get("training_feedback"),
        "acwr_ratio": fitness.get("acwr_ratio"),
        "acute_load": fitness.get("acute_load"),
        "vo2max": fitness.get("vo2max"),
        "fitness_age": age.get("fitness"),
        "race_5k": race.get("5k"),
        "race_10k": race.get("10k"),
        "race_half": race.get("half"),
        "race_full": race.get("full"),
        "weather_desc": weather.get("desc"),
        "weather_high": weather.get("high"),
        "weather_low": weather.get("low"),
        "weather_humidity": weather.get("humidity"),
        "weather_aqi": weather.get("aqi"),
        "weather_aqi_category": weather.get("aqi_category"),
        "weather_uv": weather.get("uv_index"),
        "weather_wind_kmh": weather.get("wind_kmh"),
        "weather_rain_prob": weather.get("rain_prob"),
        "location": weather.get("location"),
        "now_temp": weather.get("now_temp"),
        "now_feels_like": weather.get("now_feels_like"),
        "now_text": weather.get("now_text"),
        "now_precip": weather.get("now_precip"),
        "updated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    # 清理 None 值让 MySQL 存 NULL
    cols = {k: v for k, v in cols.items() if v is not None}

    session = SessionLocal()
    try:
        session.execute(_upsert(HealthDaily, cols, ["date", "user_id", "brand"]))

        # 同步活动
        for act in data.get("activities") or []:
            _upsert_activity(session, data["date"], act, user_id, brand)

        session.commit()
    finally:
        session.close()


def get_day(date_str: str, user_id: int = 0, brand: str | None = None) -> Optional[dict]:
    """获取某天完整数据（含活动）。brand 缺省用当前启用品牌。"""
    brand = brand or get_active_brand(user_id)
    session = SessionLocal()
    try:
        row = session.execute(
            select(HealthDaily).where(
                HealthDaily.date == date_str,
                HealthDaily.user_id == user_id,
                HealthDaily.brand == brand,
            )
        ).scalars().first()
        if not row:
            return None
        result = _activity_row(row)
        result["activities"] = _get_activities(session, date_str, user_id, brand)
        return result
    finally:
        session.close()


def get_recent(days: int = 7, user_id: int = 0, brand: str | None = None) -> list[dict]:
    """获取最近 N 天数据（含活动）。brand 缺省用当前启用品牌。"""
    brand = brand or get_active_brand(user_id)
    session = SessionLocal()
    try:
        end = dt.date.today()
        start = end - dt.timedelta(days=days - 1)
        rows = session.execute(
            select(HealthDaily).where(
                HealthDaily.date >= start.isoformat(),
                HealthDaily.date <= end.isoformat(),
                HealthDaily.user_id == user_id,
                HealthDaily.brand == brand,
            ).order_by(HealthDaily.date.desc())
        ).scalars().all()
        results = []
        for row in rows:
            d = _activity_row(row)
            d["activities"] = _get_activities(session, d["date"], user_id, brand)
            results.append(d)
        return results
    finally:
        session.close()


def get_dashboard(user_id: int = 0) -> dict:
    """仪表盘聚合数据：今日状态 + 7天趋势 + 本周摘要。"""
    today = dt.date.today().isoformat()
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()

    today_data = get_day(today, user_id) or {}
    yesterday_data = get_day(yesterday, user_id) or {}

    # 只有睡眠是"昨晚"的（滞后一天），其余字段都是今天的实时数据。
    for k in ("sleep_hours", "sleep_score", "sleep_deep_min", "sleep_rem_min",
              "sleep_light_min", "sleep_feedback"):
        if today_data.get(k) is None and yesterday_data.get(k) is not None:
            today_data[k] = yesterday_data[k]

    recent = get_recent(7, user_id)

    total_dist = 0.0
    total_dur = 0.0
    run_count = 0
    hrv_vals = []
    rhr_vals = []
    sleep_vals = []
    daily_stats = []

    for d in recent:
        for a in d.get("activities") or []:
            if a.get("distance_km"):
                total_dist += a["distance_km"]
            if a.get("duration_min"):
                total_dur += a["duration_min"]
            if a.get("type") == "running":
                run_count += 1
        if d.get("hrv"):
            hrv_vals.append({"date": d["date"], "value": d["hrv"]})
        if d.get("resting_hr"):
            rhr_vals.append({"date": d["date"], "value": d["resting_hr"]})
        if d.get("sleep_hours"):
            sleep_vals.append({"date": d["date"], "value": d["sleep_hours"]})

        daily_stats.append({
            "date": d["date"],
            "weekday": d.get("weekday", ""),
            "hrv": d.get("hrv"),
            "resting_hr": d.get("resting_hr"),
            "sleep_hours": d.get("sleep_hours"),
            "sleep_score": d.get("sleep_score"),
            "body_battery_high": d.get("body_battery_high"),
            "steps": d.get("steps"),
            "activities": [{
                "type": a.get("type"),
                "distance_km": a.get("distance_km"),
                "duration_min": a.get("duration_min"),
                "avg_hr": a.get("avg_hr"),
                "avg_pace": a.get("avg_pace"),
            } for a in d.get("activities") or []],
        })

    return {
        "today": today_data,
        "yesterday": yesterday_data,
        "recent_7d": {
            "total_distance_km": round(total_dist, 1),
            "total_duration_min": round(total_dur, 2),
            "run_count": run_count,
            "avg_hrv": round(sum(v["value"] for v in hrv_vals) / len(hrv_vals)) if hrv_vals else None,
            "avg_resting_hr": round(sum(v["value"] for v in rhr_vals) / len(rhr_vals)) if rhr_vals else None,
            "avg_sleep_h": round(sum(v["value"] for v in sleep_vals) / len(sleep_vals), 1) if sleep_vals else None,
        },
        "charts": {
            "hrv_trend": hrv_vals,
            "rhr_trend": rhr_vals,
            "sleep_trend": sleep_vals,
        },
        "daily_stats": daily_stats,
    }


def get_weather(date_str: str, user_id: int = 0, brand: str | None = None) -> Optional[dict]:
    brand = brand or get_active_brand(user_id)
    session = SessionLocal()
    try:
        row = session.execute(
            select(HealthDaily).where(
                HealthDaily.date == date_str,
                HealthDaily.user_id == user_id,
                HealthDaily.brand == brand,
            )
        ).scalars().first()
        if not row:
            return None
        return {
            "date": row.date,
            "weather_desc": row.weather_desc,
            "weather_high": row.weather_high,
            "weather_low": row.weather_low,
            "weather_humidity": row.weather_humidity,
            "weather_aqi": row.weather_aqi,
            "weather_aqi_category": row.weather_aqi_category,
            "weather_uv": row.weather_uv,
            "weather_wind_kmh": row.weather_wind_kmh,
            "weather_rain_prob": row.weather_rain_prob,
        }
    finally:
        session.close()


def get_global_day_health(date_str: str) -> Optional[dict]:
    """查某天健康数据（不过滤用户，取第一条，历史遗留语义）。"""
    session = SessionLocal()
    try:
        row = session.execute(
            select(HealthDaily.hrv, HealthDaily.sleep_hours, HealthDaily.resting_hr)
            .where(HealthDaily.date == date_str)
            .limit(1)
        ).first()
        return dict(row._mapping) if row else None
    finally:
        session.close()


def get_global_activity_count(date_str: str) -> int:
    """查某天活动总数（不过滤用户，历史遗留语义）。"""
    session = SessionLocal()
    try:
        return session.execute(
            select(func.count()).select_from(Activity).where(Activity.date == date_str)
        ).scalar() or 0
    finally:
        session.close()


# ═══════════════════════════════════════════════
# AI 每日训练计划
# ═══════════════════════════════════════════════
#
# 历史遗留：计划有两条写入路径，形状不一致——
#   - 新 Agent 工具 save_plan 传的是**数组** [day, day, day]
#   - 旧 [PLAN] 标记传的是**对象** {"days": [...]}
# 而所有读取方都写 plan.get("days")，于是存了数组之后语音链路就会
# AttributeError（C2）。这里统一在**写入口和读出口**都归一化成 {"days": [...]}，
# 两条路径随便走都不会再有形状问题。


def seed_exercise_names():
    """预填标准动作名（幂等，按 name 去重）。"""
    session = SessionLocal()
    try:
        for name, aliases, category in DEFAULT_EXERCISES:
            session.execute(
                _upsert(
                    ExerciseName,
                    {"name": name, "aliases": aliases, "category": category},
                    ["name"],
                    do_nothing=True,
                )
            )
        session.commit()
    finally:
        session.close()


def get_standard_exercise_names() -> list[str]:
    """返回所有标准动作名（供 AI 归一化参考）。"""
    session = SessionLocal()
    try:
        rows = session.execute(select(ExerciseName.name).order_by(ExerciseName.id)).all()
        return [r[0] for r in rows]
    finally:
        session.close()


# ═══════════════════════════════════════════════
# 健康数据 CRUD
# ═══════════════════════════════════════════════
