"""旧单轮对话的上下文装配。

给三条路用：`/api/chat/voice`、`WS /ws/chat`、以及 `/api/chat` 里 Agent 失败后的回退。
新的 Agent 路径**不用**它——那边是模型自己调工具查数据。

放在 services/ 而不是路由文件里，是因为它跟 HTTP 无关：输入 user_id，输出一个 dict，
唯一涉及协议的地方是它返回的字段名（`build_context()` 按这些字段拼文本）。
"""
from __future__ import annotations

import datetime as dt

from garmin_coach.db.legacy import (
    get_dashboard,
    get_latest_plan,
    get_standard_exercise_names,
    get_user_profile,
)

from .clock import period_of
from .safety import compute_safety


def build_chat_context(user_id: int = 0) -> dict:
    """构建完整的聊天上下文数据（旧链路专用）。"""
    now = dt.datetime.now()

    dashboard = get_dashboard(user_id)
    safety = compute_safety(user_id)

    # 2026-08-20：用户训练档案注入（驱动 AI 训练阶段判断 + 比赛倒计时）
    profile = get_user_profile(user_id)
    upcoming = []
    for r in (profile.get("races") or []):
        try:
            d = dt.date.fromisoformat(r["date"])
        except (KeyError, ValueError, TypeError):
            continue
        days = (d - dt.date.today()).days
        if days >= 0:
            upcoming.append({"name": r.get("name", ""), "date": r["date"], "days_until": days})
    upcoming.sort(key=lambda x: x["days_until"])
    profile_payload = {
        "goal": profile.get("goal"),
        "goal_target": profile.get("goal_target"),
        "training_types": profile.get("training_types") or [],
        "upcoming_races": upcoming,
        "soonest_race_days": upcoming[0]["days_until"] if upcoming else None,
    }

    return {
        "datetime": now.strftime("%Y-%m-%d %H:%M"),
        "period": period_of(now.hour),
        "today": dashboard.get("today") or {},
        "recent_7d": dashboard.get("recent_7d") or {},
        "daily_stats": dashboard.get("daily_stats") or [],
        "safety": safety,
        "standard_exercises": get_standard_exercise_names(),
        "plan": get_latest_plan(user_id),
        "profile": profile_payload,
    }
