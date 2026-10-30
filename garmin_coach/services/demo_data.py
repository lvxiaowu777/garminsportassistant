"""演示数据回放：让游客"点同步"就能看到一组像真实手表的数据。

原则：
- 不碰天气列（天气由前端定位 → API 写入）；
- 不碰聊天/记忆/计划；
- 同场景可重复执行（先删范围内旧数据）；
- 更新 users.last_sync_at，让"数据已同步"的状态是对的。
"""
from __future__ import annotations

import datetime as dt
import json
import random

from sqlalchemy import delete, select

from garmin_coach.db.database import SessionLocal
from garmin_coach.db.models import Activity, HealthDaily, StrengthSet, User

BRAND = "garmin"
WEEKDAY = ["一", "二", "三", "四", "五", "六", "日"]

WEATHER_COLS = (
    "weather_desc", "weather_high", "weather_low", "weather_humidity",
    "weather_aqi", "weather_aqi_category", "weather_uv", "weather_wind_kmh",
    "weather_rain_prob", "location", "now_temp", "now_feels_like", "now_text",
    "now_precip",
)

SCENARIOS: dict[str, dict] = {
    "good": {
        "title": "恢复良好（HRV 高、睡眠足、跑量稳）",
        "hrv": (62, 70), "sleep": (7.4, 8.4), "rhr": (46, 49),
        "readiness": (75, 90), "status": "有效", "acwr": (0.9, 1.1),
        "run_days": [0, 2, 5], "strength_days": [1, 4], "long_km": 18,
    },
    "fatigued": {
        "title": "疲劳堆积（HRV 低、睡眠差、负荷高）",
        "hrv": (36, 46), "sleep": (5.1, 6.3), "rhr": (55, 61),
        "readiness": (30, 52), "status": "过度", "acwr": (1.6, 1.9),
        "run_days": [0, 1, 3], "strength_days": [2], "long_km": 10,
    },
    "race_week": {
        "title": "赛前一周（减量、状态好、5 天后比赛）",
        "hrv": (55, 63), "sleep": (7.0, 8.2), "rhr": (47, 50),
        "readiness": (70, 86), "status": "巅峰", "acwr": (1.0, 1.2),
        "run_days": [0, 3], "strength_days": [2], "long_km": 8,
        "race_days": 5,
    },
    "comeback": {
        "title": "伤后恢复（跑量小、强度低）",
        "hrv": (50, 58), "sleep": (6.5, 7.8), "rhr": (50, 54),
        "readiness": (60, 75), "status": "恢复", "acwr": (0.6, 0.8),
        "run_days": [1, 4], "strength_days": [2], "long_km": 6,
    },
}


def _run_activity(date: dt.date, km: float, rng: random.Random, idx: int) -> dict:
    pace_sec = rng.randint(320, 380)
    return {
        "date": date.isoformat(), "user_id": 0, "brand": BRAND,
        "activity_id": f"replay-{date.isoformat()}-r{idx}",
        "type": "running", "time": f"{rng.randint(6, 20):02d}:{rng.randint(0, 59):02d}",
        "location": "模拟路线", "distance_km": km,
        "duration_min": round(km * pace_sec / 60, 1),
        "avg_pace": f"{pace_sec // 60}'{pace_sec % 60:02d}\"",
        "avg_hr": rng.randint(138, 158), "max_hr": rng.randint(160, 178),
        "calories": int(km * 62), "cadence_avg": round(rng.uniform(168, 182), 1),
        "elevation_gain_m": rng.randint(5, 50),
    }


def _strength_activity(date: dt.date, rng: random.Random, idx: int) -> dict:
    return {
        "date": date.isoformat(), "user_id": 0, "brand": BRAND,
        "activity_id": f"replay-{date.isoformat()}-s{idx}",
        "type": "strength_training",
        "time": f"{rng.randint(18, 21):02d}:{rng.randint(0, 59):02d}",
        "location": "家", "duration_min": rng.randint(35, 55),
        "avg_hr": rng.randint(95, 115), "calories": rng.randint(180, 300),
    }


def _strength_sets(activity_id: str, rng: random.Random) -> list[dict]:
    names = rng.sample(["深蹲", "硬拉", "卧推", "划船", "肩推", "卷腹"], 3)
    return [
        {
            "activity_id": activity_id, "name": name,
            "sets": rng.randint(3, 4), "reps": rng.randint(8, 12),
            "weight_kg": round(rng.uniform(20, 60) if name != "卷腹" else 0, 1),
        }
        for name in names
    ]


def replay(
    user_id: int,
    scenario: str = "good",
    days: int = 14,
    seed: int = 42,
    memory: bool = False,
) -> dict:
    """把某个场景写进 user 的数据；返回 {ok, scenario, days, title}。"""
    cfg = SCENARIOS.get(scenario)
    if cfg is None:
        raise ValueError(f"未知场景：{scenario}")

    rng = random.Random(seed + sum(map(ord, scenario)))
    today = dt.date.today()
    start = today - dt.timedelta(days=days - 1)

    session = SessionLocal()
    try:
        # 保留已有天气
        old_rows = session.execute(
            select(HealthDaily).where(
                HealthDaily.user_id == user_id,
                HealthDaily.date >= start.isoformat(),
            )
        ).scalars().all()
        weather_by_date = {
            r.date: {c: getattr(r, c) for c in WEATHER_COLS if getattr(r, c) is not None}
            for r in old_rows
        }

        ids = [
            r[0] for r in session.execute(
                select(Activity.activity_id).where(
                    Activity.user_id == user_id,
                    Activity.date >= start.isoformat(),
                )
            ).all()
        ]
        if ids:
            session.execute(delete(StrengthSet).where(StrengthSet.activity_id.in_(ids)))
        session.execute(delete(Activity).where(
            Activity.user_id == user_id, Activity.date >= start.isoformat()
        ))
        session.execute(delete(HealthDaily).where(
            HealthDaily.user_id == user_id, HealthDaily.date >= start.isoformat()
        ))
        session.flush()

        for offset in range(days):
            date = start + dt.timedelta(days=offset)
            hrv = round(rng.uniform(*cfg["hrv"]))
            sleep = round(rng.uniform(*cfg["sleep"]), 2)
            rhr = round(rng.uniform(*cfg["rhr"]))
            row = {
                "date": date.isoformat(), "user_id": user_id, "brand": BRAND,
                "weekday": WEEKDAY[date.weekday()],
                "sleep_hours": sleep, "sleep_score": int(sleep * 10 + rng.randint(-6, 6)),
                "sleep_deep_min": rng.randint(70, 115), "sleep_rem_min": rng.randint(60, 105),
                "resting_hr": rhr, "resting_hr_7d_avg": rhr + rng.randint(-1, 1),
                "hrv": hrv,
                "hrv_status": "BALANCED" if hrv >= 50 else rng.choice(["LOW", "POOR", "UNBALANCED"]),
                "body_battery_high": rng.randint(60, 95), "body_battery_low": rng.randint(15, 40),
                "body_battery_wake": rng.randint(50, 85), "body_battery_now": rng.randint(30, 80),
                "stress_avg": rng.randint(20, 50),
                "readiness": rng.randint(*cfg["readiness"]),
                "readiness_level": "HIGH" if cfg["readiness"][0] >= 70 else "LOW",
                "steps": rng.randint(4000, 14000), "kcal": rng.randint(1800, 2600),
                "training_status": cfg["status"], "training_feedback": "PRODUCTIVE",
                "acwr_ratio": round(rng.uniform(*cfg["acwr"]), 2),
                "vo2max": round(rng.uniform(48, 55), 1), "fitness_age": round(rng.uniform(23, 28), 1),
                "race_5k": "23:10", "race_10k": "48:30",
                "race_half": "1:46:20", "race_full": "3:52:00",
            }
            row.update(weather_by_date.get(date.isoformat(), {}))
            session.add(HealthDaily(**row))

            if offset in cfg["run_days"]:
                km = cfg["long_km"] if offset == max(cfg["run_days"]) else round(rng.uniform(5, 10), 1)
                act = _run_activity(date, km, rng, 0)
            elif offset in cfg["strength_days"]:
                act = _strength_activity(date, rng, 0)
            else:
                continue

            act["user_id"] = user_id
            act["activity_id"] = f"{act['activity_id']}-u{user_id}"
            session.add(Activity(**act))
            if act["type"] == "strength_training":
                for s in _strength_sets(act["activity_id"], rng):
                    session.add(StrengthSet(**s))

        user = session.get(User, user_id)
        if user:
            user.goal = "half_marathon"
            user.goal_target = "1:45:00"
            if scenario == "race_week":
                race_date = (today + dt.timedelta(days=cfg.get("race_days", 5))).isoformat()
                user.races = json.dumps([{"name": "模拟半马", "date": race_date}], ensure_ascii=False)
            else:
                user.races = "[]"
            # 让"数据已同步"的状态为真
            user.last_sync_at = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            user.last_sync_date = today.isoformat()

        if memory:
            from garmin_coach.agent.memory.store import upsert_memory

            upsert_memory(user_id, "goal", "race", "11 月半马，目标 sub2", confidence=0.9)
            upsert_memory(user_id, "injury", "knee", "右膝外侧疼，避免连续两天跑", confidence=0.8)

        session.commit()
        return {"ok": True, "scenario": scenario, "title": cfg["title"], "days": days}
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
