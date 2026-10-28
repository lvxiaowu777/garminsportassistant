"""安全档位服务 —— 把身体状态折算成训练强度上限。

**为什么单独一层**：HTTP 路由（网页显示）和 LLM 工具（AI 回答）都要用，
放 services 层两边共享，才能保证「网页说 EASY、AI 说 HARD」不会发生。

规则在 ``safety_rules.evaluate``（纯函数）；阈值在 ``safety_config``。
这里只负责取数、算基线、拍平成规则输入，并记一条每日快照（回测用）。
"""
from __future__ import annotations

import datetime as dt
import statistics

from garmin_coach.db.database import (
    get_day,
    get_recent,
    get_user_profile,
    save_safety_snapshot,
)

from .safety_config import THRESHOLDS
from .safety_rules import evaluate, tier_description


def estimate_hrmax(age: int | None) -> int | None:
    """Tanaka 公式：HRmax = 208 - 0.7 × 年龄（比 220-年龄 更准）。"""
    if not age or age <= 0:
        return None
    return int(round(208 - 0.7 * age))


def hrv_baseline_of(recent: list[dict], today: str) -> float | None:
    """前 N 天 HRV 的**中位数**（排除今天）。

    为什么用中位数：单日 HRV 噪声大，均值容易被异常值（生病/熬夜）拉偏。
    """
    samples = [
        d.get("hrv")
        for d in recent
        if d.get("date") != today and d.get("hrv")
    ]
    if len(samples) < THRESHOLDS.hrv_min_samples:
        return None
    return float(statistics.median(samples))


def compute_safety(user_id: int = 0) -> dict:
    """用最近数据计算安全档位。"""
    today = dt.date.today().isoformat()
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()

    today_data = get_day(today, user_id) or {}
    yesterday_data = get_day(yesterday, user_id) or {}
    base = today_data if today_data else yesterday_data

    if not base:
        tier = THRESHOLDS.no_data_tier
        return {
            "max_tier": tier,
            "tier_description": tier_description(tier),
            "blockers": [],
            "notes": ["⚠️ 今天和昨天都没有数据，按保守档处理（别直接上强度）"],
            "hrv_baseline": None,
            "hard_hr": None,
            "recent_hard_days": 0,
        }

    # 一次拿够基线窗口（降序，最近的在前面）
    recent = get_recent(THRESHOLDS.hrv_baseline_days, user_id)
    baseline = hrv_baseline_of(recent, today)

    age = (get_user_profile(user_id) or {}).get("age")
    hrmax = estimate_hrmax(age)
    hard_hr = (
        int(round(hrmax * THRESHOLDS.hr_hard_pct)) if hrmax else THRESHOLDS.fallback_hard_hr
    )

    hard_days = 0
    for d in recent[:3]:
        for a in d.get("activities") or []:
            hr = a.get("avg_hr") or 0
            dur = a.get("duration_min") or 0
            if (hr >= hard_hr and dur >= THRESHOLDS.hard_min_duration) or dur >= THRESHOLDS.long_duration:
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
        flat,
        hrv_baseline=baseline,
        recent_hard_days=hard_days,
        hard_hr=hard_hr,
    )

    # 每日快照：只记当天第一次（do_nothing），给以后"是否遵从 + 次日反馈"回测用
    if user_id:
        try:
            save_safety_snapshot(
                date_str=today,
                user_id=user_id,
                max_tier=max_tier,
                blockers=blockers,
                hard_days=hard_days,
                hrv_baseline=baseline,
                hrmax=hrmax,
            )
        except Exception:  # noqa: BLE001 - 快照失败不影响回答
            pass

    return {
        "max_tier": max_tier,
        "tier_description": tier_description(max_tier),
        "blockers": blockers,
        "notes": notes,
        "hrv_baseline": round(baseline) if baseline else None,
        "hard_hr": hard_hr,
        "recent_hard_days": hard_days,
    }


def compute_training_phase(days_until_race: int | None) -> dict:
    """训练阶段判定（距目标赛事的天数 → 当前该练什么）。"""
    if days_until_race is None:
        return {"phase": "base", "label": "基础/进展期", "focus": "跑量每周 5-10% 渐进，力量可含下肢抗阻"}
    if days_until_race > 56:
        return {"phase": "base", "label": "基础/进展期", "focus": "跑量每周 5-10% 渐进，力量可含下肢抗阻"}
    if days_until_race >= 29:
        return {"phase": "specific", "label": "专项期", "focus": "引入马拉松配速跑，力量转上肢+核心"}
    if days_until_race >= 15:
        return {"phase": "peak", "label": "巅峰维持期", "focus": "跑量封顶不再增加，力量仅核心激活，≤20min"}
    return {"phase": "taper", "label": "减量期", "focus": "跑量降至峰值 40-60%，禁止下肢力量，仅轻度核心与灵活性"}
