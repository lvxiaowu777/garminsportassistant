"""安全档位服务 —— 把身体状态折算成训练强度上限。

**为什么要单独一层**：这个函数既要被 HTTP 路由用（网页显示当前档位），
也要被 LLM 工具用（AI 回答"今晚能不能跑"）。放在 services 层两边共享，
才能保证「网页说 EASY、AI 说 HARD」这种事不会发生。

规则本身在 ``services/safety_rules.py``（纯函数，可单测），
这里只负责把数据库里的宽表拍平成规则引擎要的输入格式。
"""
from __future__ import annotations

import datetime as dt

from garmin_coach.db.database import get_day, get_recent

from .safety_rules import evaluate, tier_description


def compute_safety(user_id: int = 0) -> dict:
    """用最近数据计算安全档位。"""
    today = dt.date.today().isoformat()
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()

    today_data = get_day(today, user_id) or {}
    yesterday_data = get_day(yesterday, user_id) or {}
    base = today_data if today_data else yesterday_data

    if not base:
        return {
            "max_tier": "LONG",
            "tier_description": tier_description("LONG"),
            "blockers": [],
            "notes": ["⚠️ 无数据，无法评估，按正常状态处理"],
        }

    recent = get_recent(7, user_id)
    hrv_vals = [d.get("hrv") for d in recent if d.get("hrv")]
    recent_hrv_avg = sum(hrv_vals) / len(hrv_vals) if hrv_vals else None

    hard_days = 0
    for d in recent[:3]:
        for a in d.get("activities") or []:
            hr = a.get("avg_hr") or 0
            dur = a.get("duration_min") or 0
            if (hr >= 155 and dur >= 30) or dur >= 75:
                hard_days += 1
                break

    y_acts = [
        {
            "type": a.get("type"),
            "dist_km": a.get("distance_km"),
            "dur_min": a.get("duration_min"),
            "avg_hr": a.get("avg_hr"),
        }
        for a in (yesterday_data.get("activities") or [])
    ]

    weather = today_data.get("weather") or {}
    flat = {
        "date": base.get("date"),
        "hrv": base.get("hrv"),
        "hrv_status": base.get("hrv_status"),
        "sleep_hours": base.get("sleep_hours"),
        "sleep_score": base.get("sleep_score"),
        "resting_hr": base.get("resting_hr"),
        "body_battery_high": base.get("body_battery_high"),
        "training_readiness": base.get("readiness"),
        "training_readiness_level": base.get("readiness_level"),
        "acwr_ratio": base.get("acwr_ratio"),
        "training_feedback": base.get("training_feedback"),
        "training_status": base.get("training_status"),
        "weather": (
            {
                "high": weather.get("weather_high"),
                "humidity": weather.get("weather_humidity"),
                "aqi": weather.get("weather_aqi"),
                "aqi_category": weather.get("weather_aqi_category"),
                "uv_index": weather.get("weather_uv"),
                "rain_prob": weather.get("weather_rain_prob"),
            }
            if weather
            else {}
        ),
        "yesterday_activities": y_acts,
    }

    max_tier, blockers, notes = evaluate(
        flat, recent_hrv_avg=recent_hrv_avg, recent_hard_days=hard_days
    )

    return {
        "max_tier": max_tier,
        "tier_description": tier_description(max_tier),
        "blockers": blockers,
        "notes": notes,
        "recent_hrv_avg": round(recent_hrv_avg) if recent_hrv_avg else None,
        "recent_hard_days": hard_days,
    }


def compute_training_phase(days_until_race: int | None) -> dict:
    """训练阶段判定（距目标赛事的天数 → 当前该练什么）。

    规则来自项目里已定的阶段划分；数据里没有赛事日期时按基础期处理。
    """
    if days_until_race is None:
        return {"phase": "base", "label": "基础/进展期", "focus": "跑量每周 5-10% 渐进，力量可含下肢抗阻"}
    if days_until_race > 56:
        return {"phase": "base", "label": "基础/进展期", "focus": "跑量每周 5-10% 渐进，力量可含下肢抗阻"}
    if days_until_race >= 29:
        return {"phase": "specific", "label": "专项期", "focus": "引入马拉松配速跑，力量转上肢+核心"}
    if days_until_race >= 15:
        return {"phase": "peak", "label": "巅峰维持期", "focus": "跑量封顶不再增加，力量仅核心激活，≤20min"}
    return {"phase": "taper", "label": "减量期", "focus": "跑量降至峰值 40-60%，禁止下肢力量，仅轻度核心与灵活性"}
