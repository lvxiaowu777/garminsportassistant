"""健康数据工具集 —— Agent 的「手脚」。

每个工具的 description 都刻意写了「什么时候该调」，因为模型选不选工具主要
看这句话，而不是看参数定义。

数据查询全部复用 ``garmin_coach.db.legacy``（即旧 ``database.py``）里的函数，
**不在这里重写 SQL**。这样网页接口和 AI 工具看到的是同一份数据、同一套口径。

对照关系（工具 ← 原来的 HTTP 接口）：

    get_safety     ← /api/health/safety
    get_today      ← /api/health/dashboard
    get_recent     ← /api/health/recent/{days}
    get_day        ← /api/health/day/{date}
    get_activity   ← /api/health/activity/{id}
    get_weather    ← /api/health/weather
    get_profile    ← /api/user/profile
    record_training← /api/health/record-training
    save_plan      ← （原来靠模型输出 [PLAN] 标记再正则解析）
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
from typing import Any

from pydantic import BaseModel, Field

from garmin_coach.db.legacy import (
    get_activity as db_get_activity,
    get_day as db_get_day,
    get_latest_plan as db_get_latest_plan,
    get_recent as db_get_recent,
    get_user_profile as db_get_user_profile,
    get_weather as db_get_weather,
    record_training as db_record_training,
    save_plan as db_save_plan,
)
from garmin_coach.services.safety import compute_safety, compute_training_phase

from .base import Tool, ToolResult


# ────────────────────────────── 参数模型 ──────────────────────────────


class EmptyParams(BaseModel):
    """无参数。"""


class DaysParams(BaseModel):
    days: int = Field(7, ge=1, le=30, description="往回看多少天，默认 7")


class DateParams(BaseModel):
    date: str = Field("", description="日期 YYYY-MM-DD；留空表示今天")


class ActivityParams(BaseModel):
    activity_id: str = Field(..., description="活动 ID，从 recent / day 的返回里拿")


class ExerciseItem(BaseModel):
    name: str = Field(..., description="标准动作名，如「深蹲」")
    sets: int | None = Field(None, description="组数")
    reps: int | None = Field(None, description="每组次数（各组相同时用）")
    reps_detail: str | None = Field(
        None, description="每组次数明细，如 15/10/10/10（与 reps 二选一）"
    )
    weight_kg: float | None = Field(None, description="重量 kg")
    equipment: str | None = Field(None, description="器械，如「哑铃 10kg」")


class RecordParams(BaseModel):
    train_type: str = Field(
        ...,
        description="训练类型：strength_training / running / cycling / swimming",
    )
    duration_min: int = Field(..., ge=1, le=600, description="时长（分钟）")
    notes: str = Field("", description="训练内容摘要，力量写动作和组数")
    exercises: list[ExerciseItem] | None = Field(
        None, description="力量训练的动作明细，跑步/骑行不用传"
    )


class PlanDay(BaseModel):
    date: str = Field(..., description="YYYY-MM-DD")
    day: str = Field(..., description="今天 / 明天 / 后天")
    run: str = Field(..., description="跑步安排，如 easy 6km 心率<140；休息写 rest")
    strength: str = Field("", description="力量安排，没有写空字符串")
    note: str = Field("", description="当天注意事项")


class PlanParams(BaseModel):
    days: list[PlanDay] = Field(..., description="未来 3 天的计划")


# ────────────────────────────── 格式化辅助 ──────────────────────────────


def _g(d: dict, key: str, default: Any = None) -> Any:
    """安全取字段：None 和空字符串都当作「没数据」。"""
    value = d.get(key, default)
    return default if value in (None, "", []) else value


async def _in_thread(fn, *args, **kwargs):
    """把同步的 SQLAlchemy 查询丢到线程池执行。

    为什么必须这么做：``database.py`` 用的是同步 Session，直接在协程里调用会
    阻塞整个事件循环 —— 一个慢查询能把所有人的请求都卡住。工具是 async 接口
    但底层是同步 IO，这里就是两者的接缝。
    """
    return await asyncio.to_thread(fn, *args, **kwargs)


def _fmt_activity_brief(a: dict) -> str:
    """活动的紧凑一行。

    **必须带 activity_id** —— 模型要靠它才能进一步调 get_activity 拿分段详情。
    第一版漏了这个字段，结果模型只能拿日期去猜 activity_id，连着两次查不到。
    """
    parts: list[str] = []
    aid = a.get("activity_id") or a.get("id")
    if aid:
        parts.append(f"id={aid}")
    parts.append(str(a.get("type") or "活动"))
    if _g(a, "distance_km"):
        parts.append(f"{a['distance_km']}km")
    if _g(a, "duration_min"):
        parts.append(f"{a['duration_min']}min")
    if _g(a, "avg_pace"):
        parts.append(f"配速{a['avg_pace']}")
    if _g(a, "avg_hr"):
        parts.append(f"心率{a['avg_hr']}/{a.get('max_hr', '?')}")
    return " ".join(parts)


def _fmt_day(d: dict) -> str:
    """一天的紧凑一行。"""
    bits = [d.get("date", "?")]
    if _g(d, "weekday"):
        bits.append(f"周{d['weekday']}")
    if _g(d, "hrv"):
        bits.append(f"HRV{d['hrv']}")
    if _g(d, "resting_hr"):
        bits.append(f"静息{d['resting_hr']}")
    if _g(d, "sleep_hours"):
        bits.append(f"睡眠{d['sleep_hours']}h")
    acts = d.get("activities") or []
    for a in acts:
        bits.append(f"[{_fmt_activity_brief(a)}]")
    if len(bits) == 1:
        bits.append("无数据")
    return " ".join(str(b) for b in bits)


# ────────────────────────────── 工具实现 ──────────────────────────────


class GetSafetyTool(Tool):
    name = "get_safety"
    description = (
        "查询当前的安全档位 —— 身体状态允许的最大训练强度，这是硬约束。\n"
        "**给出任何训练建议之前必须先调用**。返回 max_tier（REST/EASY/MODERATE/"
        "HARD/LONG）、档位说明、以及触发降档的具体原因。"
    )
    params_model = EmptyParams

    def __init__(self, user_id: int = 0) -> None:
        self._user_id = user_id

    async def execute(self, params: BaseModel) -> ToolResult:
        safety = await _in_thread(compute_safety, self._user_id)
        lines = [f"安全档位: {safety['max_tier']}（{safety.get('tier_description', '')}）"]
        blockers = safety.get("blockers") or []
        if blockers:
            lines.append("降档原因: " + "; ".join(blockers))
        notes = safety.get("notes") or []
        if notes:
            lines.append("注意: " + "; ".join(notes))
        if safety.get("recent_hrv_avg"):
            lines.append(f"近 7 天平均 HRV: {safety['recent_hrv_avg']}")
        if safety.get("recent_hard_days") is not None:
            lines.append(f"近 3 天高强度次数: {safety['recent_hard_days']}")
        return ToolResult("\n".join(lines))


class GetTodayTool(Tool):
    name = "get_today"
    description = (
        "查询今天的恢复状态、睡眠、训练状态、今日运动和天气。\n"
        "用户问「今天状态怎么样」「今晚能不能跑」时调它。"
    )
    params_model = EmptyParams

    def __init__(self, user_id: int = 0) -> None:
        self._user_id = user_id

    async def execute(self, params: BaseModel) -> ToolResult:
        today = dt.date.today().isoformat()
        d = await _in_thread(db_get_day, today, self._user_id) or {}
        if not d:
            return ToolResult(f"{today} 还没有数据（可能今天还没同步手表）")

        lines = [f"日期: {today}（今天）"]
        recovery = []
        for key, label in (
            ("hrv", "HRV"),
            ("hrv_status", "HRV 状态"),
            ("resting_hr", "静息心率"),
            ("body_battery_high", "Body Battery"),
            ("readiness", "训练准备度"),
            ("training_status", "训练状态"),
            ("acwr_ratio", "ACWR"),
        ):
            if _g(d, key):
                recovery.append(f"{label} {d[key]}")
        if recovery:
            lines.append("恢复: " + ", ".join(str(x) for x in recovery))

        sleep = []
        for key, label in (
            ("sleep_hours", "时长"),
            ("sleep_score", "得分"),
            ("sleep_deep_min", "深睡"),
            ("sleep_rem_min", "REM"),
        ):
            if _g(d, key):
                sleep.append(f"{label} {d[key]}")
        if sleep:
            lines.append("睡眠: " + ", ".join(str(x) for x in sleep))

        weather = []
        for key, label in (
            ("weather_desc", "天气"),
            ("weather_high", "最高"),
            ("weather_low", "最低"),
            ("weather_aqi", "AQI"),
            ("now_temp", "当前温度"),
        ):
            if _g(d, key):
                weather.append(f"{label} {d[key]}")
        if weather:
            lines.append("天气: " + ", ".join(str(x) for x in weather))

        acts = d.get("activities") or []
        if acts:
            lines.append("今日运动: " + "; ".join(_fmt_activity_brief(a) for a in acts))
        else:
            lines.append("今日运动: 无")
        return ToolResult("\n".join(lines))


class GetRecentTool(Tool):
    name = "get_recent"
    description = (
        "查询最近 N 天的逐日数据（恢复、睡眠、每天的运动）。\n"
        "用户问「这周跑量多少」「最近恢复得怎么样」时调它。"
        "days 默认 7，最大 30。"
    )
    params_model = DaysParams

    def __init__(self, user_id: int = 0) -> None:
        self._user_id = user_id

    async def execute(self, params: BaseModel) -> ToolResult:
        p: DaysParams = params  # type: ignore[assignment]
        rows = await _in_thread(db_get_recent, p.days, self._user_id)
        if not rows:
            return ToolResult(f"最近 {p.days} 天没有任何数据")

        total_km = sum(r.get("distance_km") or 0 for r in rows)
        run_count = sum(1 for r in rows if (r.get("distance_km") or 0) > 0)
        hrv = [r["hrv"] for r in rows if r.get("hrv")]
        rhr = [r["resting_hr"] for r in rows if r.get("resting_hr")]
        sleep = [r["sleep_hours"] for r in rows if r.get("sleep_hours")]

        lines = [f"最近 {p.days} 天："]
        lines.append(f"总跑量 {round(total_km, 1)}km，跑步 {run_count} 次")
        if hrv:
            lines.append(f"平均 HRV {round(sum(hrv) / len(hrv))}")
        if rhr:
            lines.append(f"平均静息心率 {round(sum(rhr) / len(rhr))}")
        if sleep:
            lines.append(f"平均睡眠 {round(sum(sleep) / len(sleep), 1)}h")
        lines.append("逐日：")
        for r in rows:
            lines.append("  " + _fmt_day(r))
        return ToolResult("\n".join(lines))


class GetDayTool(Tool):
    name = "get_day"
    description = (
        "查询指定日期的详细数据。用户提到某个具体日期（「8 月 14 号那次跑步」"
        "「上周三」）时用，date 传 YYYY-MM-DD；不传就是今天。"
    )
    params_model = DateParams

    def __init__(self, user_id: int = 0) -> None:
        self._user_id = user_id

    async def execute(self, params: BaseModel) -> ToolResult:
        p: DateParams = params  # type: ignore[assignment]
        date_str = p.date.strip() or dt.date.today().isoformat()
        d = await _in_thread(db_get_day, date_str, self._user_id)
        if not d:
            return ToolResult(f"{date_str} 没有数据")

        lines = [_fmt_day(d)]
        notes = _g(d, "notes")
        if notes:
            lines.append(f"备注: {notes}")
        return ToolResult("\n".join(lines))


class GetActivityTool(Tool):
    name = "get_activity"
    description = (
        "查询单次活动的完整细节：每公里分段配速、心率区间分布、训练效果。\n"
        "用户问「那次跑步每公里配速多少」「心率区间怎么样」时用。"
        "activity_id 从 get_recent 或 get_day 的返回里拿。"
    )
    params_model = ActivityParams

    def __init__(self, user_id: int = 0) -> None:
        self._user_id = user_id

    async def execute(self, params: BaseModel) -> ToolResult:
        p: ActivityParams = params  # type: ignore[assignment]
        a = await _in_thread(db_get_activity, p.activity_id, self._user_id)
        if not a:
            return ToolResult(f"找不到活动 {p.activity_id}")

        lines = [
            f"活动 {p.activity_id}",
            f"  类型 {a.get('type')}  日期 {a.get('date')} {a.get('time') or ''}"
            f"  地点 {_g(a, 'location', '未知')}",
            f"  距离 {a.get('distance_km')}km  用时 {a.get('duration_min')}min"
            f"  平均配速 {_g(a, 'avg_pace', '?')}  心率 {a.get('avg_hr')}/{a.get('max_hr')}",
        ]
        if _g(a, "training_effect_aerobic"):
            lines.append(f"  有氧训练效果 {a['training_effect_aerobic']}")

        zones = a.get("hr_zones")
        if zones:
            if isinstance(zones, str):
                try:
                    zones = json.loads(zones)
                except json.JSONDecodeError:
                    zones = None
            if isinstance(zones, dict):
                # 只取 *_sec 的时长字段；*_low_bpm 是区间下界，对模型是噪声
                zt = ", ".join(
                    f"{k.replace('_sec', '').upper()} {v}s"
                    for k, v in zones.items()
                    if k.endswith("_sec") and v
                )
                if zt:
                    lines.append(f"  心率区间: {zt}")

        splits = a.get("splits")
        if splits:
            if isinstance(splits, str):
                try:
                    splits = json.loads(splits)
                except json.JSONDecodeError:
                    splits = None
            if isinstance(splits, list) and splits:
                rendered = []
                for s in splits[:12]:
                    seg = f"{s.get('km', '?')}km"
                    if s.get("pace"):
                        seg += f" {s['pace']}"
                    if s.get("hr"):
                        seg += f" HR{s['hr']}"
                    rendered.append(seg)
                lines.append("  分段: " + " | ".join(rendered))
        return ToolResult("\n".join(lines))


class GetWeatherTool(Tool):
    name = "get_weather"
    description = "查询指定日期的天气（温度、湿度、AQI、降水概率）。安排户外训练时用。"
    params_model = DateParams

    def __init__(self, user_id: int = 0) -> None:
        self._user_id = user_id

    async def execute(self, params: BaseModel) -> ToolResult:
        p: DateParams = params  # type: ignore[assignment]
        date_str = p.date.strip() or dt.date.today().isoformat()
        w = await _in_thread(db_get_weather, date_str, self._user_id)
        if not w:
            return ToolResult(f"{date_str} 没有天气数据")
        parts = []
        for key, label in (
            ("weather_desc", "天气"),
            ("weather_high", "最高温"),
            ("weather_low", "最低温"),
            ("weather_humidity", "湿度"),
            ("weather_aqi", "AQI"),
            ("weather_aqi_category", "空气质量"),
            ("weather_uv", "UV"),
            ("weather_wind_kmh", "风速"),
            ("weather_rain_prob", "降水概率"),
        ):
            if _g(w, key):
                parts.append(f"{label} {w[key]}")
        return ToolResult(f"{date_str}：" + ", ".join(parts) if parts else f"{date_str} 无有效天气")


class GetProfileTool(Tool):
    name = "get_profile"
    description = (
        "查询用户的训练档案：目标赛事、目标成绩、训练类型、未来比赛倒计时，"
        "以及当前所处的训练阶段。\n"
        "**做中长期训练安排前调它**，短问答不用。"
    )
    params_model = EmptyParams

    def __init__(self, user_id: int = 0) -> None:
        self._user_id = user_id

    async def execute(self, params: BaseModel) -> ToolResult:
        profile = await _in_thread(db_get_user_profile, self._user_id) or {}
        lines = []

        goal_label = {
            "half_marathon": "半马",
            "full_marathon": "全马",
            "shaping": "塑形",
        }.get(profile.get("goal") or "", profile.get("goal") or "未设置")
        lines.append(f"训练目标: {goal_label}")
        if profile.get("goal_target"):
            lines.append(f"目标成绩: {profile['goal_target']}")

        types = profile.get("training_types") or []
        if types:
            lines.append("训练类型: " + " / ".join(types))

        races = profile.get("races") or []
        upcoming = []
        for r in races:
            try:
                d = dt.date.fromisoformat(r["date"])
            except (KeyError, ValueError, TypeError):
                continue
            days = (d - dt.date.today()).days
            if days >= 0:
                upcoming.append((days, r.get("name", ""), r["date"]))
        upcoming.sort()
        if upcoming:
            lines.append(f"未来比赛 {len(upcoming)} 场：")
            for days, name, date in upcoming[:5]:
                lines.append(f"  {name} @ {date}（还有 {days} 天）")
        else:
            lines.append("未来比赛: 无")

        phase = compute_training_phase(upcoming[0][0] if upcoming else None)
        lines.append(f"当前训练阶段: {phase['label']} —— {phase['focus']}")

        plan = await _in_thread(db_get_latest_plan, self._user_id)
        if plan and plan.get("days"):
            lines.append("已生成的训练计划（安排今日时优先参考）：")
            for d in plan["days"][:3]:
                lines.append(
                    f"  {d.get('date', '')} {d.get('day', '')}: 跑步 {d.get('run', '休息')}"
                    f" / 力量 {d.get('strength') or '无'} / {d.get('note') or ''}"
                )
        return ToolResult("\n".join(lines))


class RecordTrainingTool(Tool):
    name = "record_training"
    description = (
        "把用户口述的一次训练写进记录。\n"
        "**只在这两种情况调用**：(1) 用户明确说自己练了什么（「今天练了力量，"
        "深蹲 5 组」）；(2) 用户明确要求记录。问建议、看数据、普通聊天都不要调。\n"
        "调用前先从对话里提取出类型、时长、动作明细；用户没说的字段留空。"
    )
    params_model = RecordParams

    def __init__(self, user_id: int = 0) -> None:
        self._user_id = user_id

    async def execute(self, params: BaseModel) -> ToolResult:
        p: RecordParams = params  # type: ignore[assignment]
        exercises = [e.model_dump(exclude_none=True) for e in (p.exercises or [])]
        ok = await _in_thread(db_record_training, 
            self._user_id,
            p.train_type,
            p.duration_min,
            p.notes,
            exercises=exercises or None,
        )
        if not ok:
            return ToolResult("写入训练记录失败", is_error=True)
        return ToolResult(
            f"已记录：{p.train_type} {p.duration_min}min"
            + (f"，{len(exercises)} 个动作" if exercises else "")
        )


class SavePlanTool(Tool):
    name = "save_plan"
    description = (
        "保存未来几天的训练计划，供后续对话引用。\n"
        "**只在用户明确要求安排未来计划时调用**（「未来三天怎么练」「帮我安排一下」），"
        "并且要在你已经用自然语言把计划讲清楚之后再调。"
    )
    params_model = PlanParams

    def __init__(self, user_id: int = 0) -> None:
        self._user_id = user_id

    async def execute(self, params: BaseModel) -> ToolResult:
        p: PlanParams = params  # type: ignore[assignment]
        payload = [d.model_dump() for d in p.days]
        ok = await _in_thread(db_save_plan, self._user_id, json.dumps(payload, ensure_ascii=False))
        if not ok:
            return ToolResult("保存训练计划失败", is_error=True)
        return ToolResult(f"已保存 {len(payload)} 天计划")


def build_default_registry(user_id: int = 0):
    """构造这个用户可用的工具集。

    每个工具都绑定 user_id —— 工具层不做鉴权，但必须**带上身份**，
    否则多用户场景下会读到别人的数据。这是比 HTTP 路由更容易出错的点：
    路由有 Depends(get_current_user) 兜着，工具是 Agent 内部调的。
    """
    from .registry import ToolRegistry

    registry = ToolRegistry()
    for tool_cls in (
        GetSafetyTool,
        GetTodayTool,
        GetRecentTool,
        GetDayTool,
        GetActivityTool,
        GetWeatherTool,
        GetProfileTool,
        RecordTrainingTool,
        SavePlanTool,
    ):
        registry.register(tool_cls(user_id=user_id))
    return registry
