"""
Garmin 数据同步 —— 拉指定日期的手表数据，落到知识库/健康/YYYY-MM/YYYY-MM-DD.md

v2 (2026-07-10)：一次拉全量
- 每日：睡眠 / HRV / 静息心率 / Body Battery / 压力 / 步数 / 卡路里
- 每个活动：地点 / 每公里分段 / 心率区间 / 训练效果 / VO2Max / 步频 / 爬升 / 卡路里
- 数据 md 是唯一真相，之后 CLI 全从这里读

用法：
    python garmin_sync.py                # 默认拉昨天
    python garmin_sync.py 2026-07-08     # 指定日期
    python garmin_sync.py --backfill 7   # 补拉最近 7 天
"""

import os
import sys
import json
import argparse
import datetime as dt
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
)

from frontmatter import format as format_frontmatter
from frontmatter import parse as parse_frontmatter

# 路径常量
PROJECT_DIR = Path(__file__).resolve().parent.parent
TOKEN_DIR = PROJECT_DIR / "config" / ".tokens"
STUDY_DIR = Path(os.environ.get("GARMIN_STUDY_DIR") or (Path.home() / "study"))
HEALTH_DIR = STUDY_DIR / "知识库" / "健康"

_CLIENT_CACHE = None


def get_client() -> Garmin:
    """用缓存的 token 登录。同进程内只登一次。"""
    global _CLIENT_CACHE
    if _CLIENT_CACHE is not None:
        return _CLIENT_CACHE
    if not TOKEN_DIR.exists() or not any(TOKEN_DIR.iterdir()):
        raise RuntimeError(
            f"Garmin token 不存在: {TOKEN_DIR}\n"
            f"请先运行: python src/init_garmin.py"
        )
    is_cn = True
    config_file = TOKEN_DIR / "config.json"
    if config_file.exists():
        try:
            is_cn = bool(json.loads(config_file.read_text(encoding="utf-8")).get("is_cn", True))
        except Exception:
            pass
    g = Garmin(is_cn=is_cn)
    g.login(str(TOKEN_DIR))
    _CLIENT_CACHE = g
    return g


def _safe(fn, *args, default=None, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        print(f"  ⚠️  {fn.__name__} 挂了: {type(e).__name__}: {str(e)[:120]}")
        return default


# ═══════════════════════════════════════════════
# 单次活动的完整数据抽取
# ═══════════════════════════════════════════════

def _pace_str(avg_speed_ms: float | None) -> str | None:
    """m/s → "min'sec"/km"""
    if not avg_speed_ms:
        return None
    total_sec_per_km = 1000 / avg_speed_ms
    m = int(total_sec_per_km // 60)
    s = int(round(total_sec_per_km - m * 60))
    if s >= 60:
        m += 1
        s = 0
    return f"{m}'{s:02d}\""


def fetch_activity_full(g: Garmin, act_summary: dict) -> dict:
    """
    从活动概要拉出完整数据：分段、心率区间、训练效果、地点、步频等。
    act_summary 是 get_activities_by_date 返回的其中一条。
    """
    aid = act_summary.get("activityId")
    if not aid:
        return {}

    typ = (act_summary.get("activityType") or {}).get("typeKey", "")
    start_local = act_summary.get("startTimeLocal", "")

    result = {
        "id": aid,
        "time": start_local[11:16] if len(start_local) >= 16 else start_local,
        "type": typ,
        "location": act_summary.get("locationName"),
        "distance_km": round((act_summary.get("distance") or 0) / 1000, 2),
        "duration_min": round((act_summary.get("duration") or 0) / 60, 2),
        "moving_duration_min": round((act_summary.get("movingDuration") or 0) / 60, 2),
        "avg_hr": round(act_summary.get("averageHR")) if act_summary.get("averageHR") else None,
        "max_hr": round(act_summary.get("maxHR")) if act_summary.get("maxHR") else None,
        "avg_pace": _pace_str(act_summary.get("averageSpeed")),
        "max_pace": _pace_str(act_summary.get("maxSpeed")),
        "elevation_gain_m": round(act_summary.get("elevationGain") or 0, 1),
        "elevation_loss_m": round(act_summary.get("elevationLoss") or 0, 1),
        "calories": round(act_summary.get("calories")) if act_summary.get("calories") else None,
        "cadence_avg": round(act_summary.get("averageRunCadence"), 1) if act_summary.get("averageRunCadence") else None,
        "cadence_max": round(act_summary.get("maxRunCadence"), 1) if act_summary.get("maxRunCadence") else None,
        "stride_length_cm": round(act_summary.get("avgStrideLength"), 1) if act_summary.get("avgStrideLength") else None,
        "training_effect_aerobic": round(act_summary.get("aerobicTrainingEffect"), 1) if act_summary.get("aerobicTrainingEffect") else None,
        "training_effect_anaerobic": round(act_summary.get("anaerobicTrainingEffect"), 1) if act_summary.get("anaerobicTrainingEffect") else None,
        "training_load": round(act_summary.get("activityTrainingLoad")) if act_summary.get("activityTrainingLoad") else None,
        "vo2max": round(act_summary.get("vO2MaxValue")) if act_summary.get("vO2MaxValue") else None,
        "temperature_c": act_summary.get("minTemperature"),
        "steps": act_summary.get("steps"),
    }

    # 每公里分段
    splits = _safe(g.get_activity_splits, aid, default=None)
    if splits and "lapDTOs" in splits:
        laps = []
        for lap in splits["lapDTOs"]:
            laps.append({
                "km": lap.get("lapIndex"),
                "dist_km": round((lap.get("distance") or 0) / 1000, 2),
                "dur_sec": round(lap.get("duration") or 0),
                "pace": _pace_str(lap.get("averageSpeed")),
                "hr": round(lap.get("averageHR")) if lap.get("averageHR") else None,
                "max_hr": round(lap.get("maxHR")) if lap.get("maxHR") else None,
                "elev_gain_m": round(lap.get("elevationGain") or 0, 1),
                "cadence": round(lap.get("averageRunCadence") or 0, 1) if lap.get("averageRunCadence") else None,
            })
        result["splits"] = laps

    # 心率区间
    hrz = _safe(g.get_activity_hr_in_timezones, aid, default=None)
    if hrz:
        result["hr_zones"] = {}
        for z in hrz:
            zn = z.get("zoneNumber")
            secs = z.get("secsInZone", 0) or 0
            if zn is not None:
                result["hr_zones"][f"z{zn}_sec"] = round(secs)
                result["hr_zones"][f"z{zn}_low_bpm"] = z.get("zoneLowBoundary")

    return result


# ═══════════════════════════════════════════════
# 一天的完整拉取
# ═══════════════════════════════════════════════

def fetch_day(g: Garmin, date_str: str) -> dict:
    print(f"→ 拉 {date_str} 的数据...")
    data = {"date": date_str}

    # 星期几
    d = dt.date.fromisoformat(date_str)
    data["weekday"] = ["一", "二", "三", "四", "五", "六", "日"][d.weekday()]

    # ── sleep ──
    sleep_raw = _safe(g.get_sleep_data, date_str, default={}) or {}
    daily_sleep = sleep_raw.get("dailySleepDTO", {}) or {}
    sleep_secs = daily_sleep.get("sleepTimeSeconds") or 0
    sleep = {}
    if sleep_secs:
        sleep["hours"] = round(sleep_secs / 3600, 2)
    score = (daily_sleep.get("sleepScores") or {}).get("overall", {}).get("value")
    if score:
        sleep["score"] = score
    if daily_sleep.get("deepSleepSeconds"):
        sleep["deep_min"] = round(daily_sleep["deepSleepSeconds"] / 60)
    if daily_sleep.get("remSleepSeconds"):
        sleep["rem_min"] = round(daily_sleep["remSleepSeconds"] / 60)
    if daily_sleep.get("lightSleepSeconds"):
        sleep["light_min"] = round(daily_sleep["lightSleepSeconds"] / 60)
    # 醒着的时间（浅睡里的一部分，反映睡眠质量）
    if daily_sleep.get("awakeSleepSeconds"):
        sleep["awake_min"] = round(daily_sleep["awakeSleepSeconds"] / 60)
    # 各阶段占比 qualifier（EXCELLENT / FAIR 等）
    ss = daily_sleep.get("sleepScores") or {}
    if ss.get("remPercentage", {}).get("value"):
        sleep["rem_pct"] = ss["remPercentage"]["value"]
    if ss.get("deepPercentage", {}).get("value"):
        sleep["deep_pct"] = ss["deepPercentage"]["value"]
    if ss.get("lightPercentage", {}).get("value"):
        sleep["light_pct"] = ss["lightPercentage"]["value"]
    # 睡眠期间的生理指标（跟醒着不一样，更能反映恢复）
    if daily_sleep.get("averageRespirationValue"):
        sleep["respiration_avg"] = daily_sleep["averageRespirationValue"]
    if daily_sleep.get("avgSleepStress") is not None:
        sleep["sleep_stress_avg"] = daily_sleep["avgSleepStress"]
    if daily_sleep.get("avgOvernightHrv"):
        sleep["overnight_hrv"] = daily_sleep["avgOvernightHrv"]
    if daily_sleep.get("restlessMomentCount"):
        sleep["restless_moments"] = daily_sleep["restlessMomentCount"]
    # 官方文字反馈（POSITIVE_LONG_AND_REFRESHING 等）
    if daily_sleep.get("sleepScoreFeedback"):
        sleep["feedback"] = daily_sleep["sleepScoreFeedback"]
    if daily_sleep.get("sleepScoreInsight"):
        sleep["insight"] = daily_sleep["sleepScoreInsight"]
    if sleep:
        data["sleep"] = sleep

    # ── stats / recovery ──
    stats = _safe(g.get_stats, date_str, default={}) or {}
    recovery = {}
    for src_key, dst_key in [
        ("restingHeartRate", "resting_hr"),
        ("lastSevenDaysAvgRestingHeartRate", "resting_hr_7d_avg"),  # 7 天均值，对比今日
        ("bodyBatteryHighestValue", "body_battery_high"),
        ("bodyBatteryLowestValue", "body_battery_low"),
        ("bodyBatteryAtWakeTime", "body_battery_wake"),          # 醒来时的 BB
        ("bodyBatteryChargedValue", "body_battery_charged"),     # 一天充了多少
        ("bodyBatteryDrainedValue", "body_battery_drained"),     # 一天消耗多少
        ("bodyBatteryDuringSleep", "body_battery_sleep_gain"),   # 睡眠期充的
        ("averageStressLevel", "stress_avg"),
        ("maxStressLevel", "stress_max"),
        ("stressQualifier", "stress_qualifier"),                 # CALM / BALANCED / STRESSFUL
        ("minHeartRate", "hr_min_day"),                          # 全天心率最低
        ("maxHeartRate", "hr_max_day"),                          # 全天心率最高
    ]:
        v = stats.get(src_key)
        if v is not None:
            recovery[dst_key] = v

    # 当前瞬时 Body Battery（bodyBatteryValuesArray 最后一点，用于「醒时+当前」双值展示）
    try:
        bb = _safe(g.get_body_battery, date_str, default=None)
        if isinstance(bb, list) and bb and isinstance(bb[0], dict):
            values = bb[0].get("bodyBatteryValuesArray") or []
            if values:
                last = values[-1]
                if isinstance(last, dict):
                    recovery["body_battery_now"] = last.get("value")
                elif isinstance(last, (list, tuple)) and len(last) >= 2:
                    recovery["body_battery_now"] = last[1]
    except Exception:
        pass

    hrv_raw = _safe(g.get_hrv_data, date_str, default=None)
    if hrv_raw and isinstance(hrv_raw, dict):
        hs = hrv_raw.get("hrvSummary") or {}
        if hs.get("lastNightAvg"):
            recovery["hrv"] = hs["lastNightAvg"]
        if hs.get("status"):
            recovery["hrv_status"] = hs["status"]
        if hs.get("lastNight5MinHigh"):
            recovery["hrv_5min_high"] = hs["lastNight5MinHigh"]
        # 你的 HRV 基线区间（能判断今日是否偏离个人正常）
        bl = hs.get("baseline") or {}
        if bl.get("balancedLow") and bl.get("balancedUpper"):
            recovery["hrv_baseline_low"] = bl["balancedLow"]
            recovery["hrv_baseline_high"] = bl["balancedUpper"]
        if hs.get("feedbackPhrase"):
            recovery["hrv_feedback"] = hs["feedbackPhrase"]

    tr = _safe(g.get_training_readiness, date_str, default=None)
    if tr and isinstance(tr, list) and tr:
        recovery["readiness"] = tr[0].get("score")
        recovery["readiness_level"] = tr[0].get("level")
        # readiness 里可能还有 feedback / factor
        if tr[0].get("feedbackLong"):
            recovery["readiness_feedback"] = tr[0].get("feedbackLong")

    if recovery:
        data["recovery"] = recovery

    # ── daily ──
    daily = {}
    for src_key, dst_key in [
        ("totalSteps", "steps"),
        ("dailyStepGoal", "step_goal"),
        ("totalKilocalories", "kcal"),
        ("floorsAscended", "floors"),
        ("floorsDescended", "floors_desc"),
        ("moderateIntensityMinutes", "moderate_min"),
        ("vigorousIntensityMinutes", "vigorous_min"),
        ("intensityMinutesGoal", "intensity_goal"),
    ]:
        v = stats.get(src_key)
        if v is not None:
            daily[dst_key] = round(v) if isinstance(v, float) else v
    # 完成率
    if daily.get("steps") and daily.get("step_goal"):
        daily["step_goal_pct"] = round(daily["steps"] * 100 / daily["step_goal"])

    # user_summary 的压力时长细分（比 stress_avg 有用得多）
    us = _safe(g.get_user_summary, date_str, default={}) or {}
    for src_key, dst_key in [
        ("activeKilocalories", "active_kcal"),
        ("bmrKilocalories", "bmr_kcal"),
        ("totalDistanceMeters", "distance_m"),
        ("activeSeconds", "active_sec"),
        ("highlyActiveSeconds", "highly_active_sec"),
        ("sedentarySeconds", "sedentary_sec"),
        ("sleepingSeconds", "sleeping_sec"),
    ]:
        v = us.get(src_key)
        if v is not None:
            daily[dst_key] = round(v) if isinstance(v, float) else v

    # 压力时长细分（换算成小时更好读）
    stress_breakdown = {}
    for src_key, dst_key in [
        ("restStressDuration", "rest_h"),
        ("lowStressDuration", "low_h"),
        ("mediumStressDuration", "medium_h"),
        ("highStressDuration", "high_h"),
        ("activityStressDuration", "activity_h"),
    ]:
        v = us.get(src_key)
        if v is not None and v > 0:
            stress_breakdown[dst_key] = round(v / 3600, 2)
    if stress_breakdown:
        daily["stress_breakdown"] = stress_breakdown

    if daily:
        data["daily"] = daily

    # ── fitness (VO2Max, race predictions, training status) ──
    fitness = {}

    max_metrics = _safe(g.get_max_metrics, date_str, default=None)
    if max_metrics and isinstance(max_metrics, list) and max_metrics:
        gen = (max_metrics[0] or {}).get("generic") or {}
        vo2 = gen.get("vo2MaxPreciseValue") or gen.get("vo2MaxValue")
        if vo2:
            fitness["vo2max"] = round(vo2, 1)
        if gen.get("fitnessAge"):
            fitness["fitness_age"] = gen["fitnessAge"]

    tr_status = _safe(g.get_training_status, date_str, default=None)
    if tr_status and isinstance(tr_status, dict):
        # 训练状态：mostRecentTrainingStatus.latestTrainingStatusData.<deviceId>.*
        lts = (tr_status.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData") or {}
        if isinstance(lts, dict) and lts:
            # 取第一个 device 的数据（一般只有一个主设备）
            first_dev = next(iter(lts.values()), None)
            if isinstance(first_dev, dict):
                # 训练状态编码 → 可读文本
                _STATUS_TEXT = {
                    0: "无状态", 1: "停训", 2: "恢复", 3: "保持",
                    4: "有效", 5: "巅峰", 6: "过度训练", 7: "无效", 8: "紧张",
                }
                st_code = first_dev.get("trainingStatus")
                if st_code is not None:
                    fitness["training_status"] = _STATUS_TEXT.get(st_code, f"未知({st_code})")
                    fitness["training_status_code"] = st_code
                phrase = first_dev.get("trainingStatusFeedbackPhrase")
                if phrase:
                    fitness["training_feedback"] = phrase
                if first_dev.get("fitnessTrend") is not None:
                    # 趋势编码：一般 1=下降 2=持平 3=上升
                    _TREND = {1: "下降", 2: "持平", 3: "上升"}
                    fitness["fitness_trend"] = _TREND.get(first_dev["fitnessTrend"], first_dev["fitnessTrend"])
                # 急性负荷
                atl = first_dev.get("acuteTrainingLoadDTO") or {}
                if isinstance(atl, dict):
                    for src, dst in [
                        ("acuteTrainingLoad", "acute_load"),
                        ("acwrPercent", "acwr_pct"),   # 急慢性比 —— 受伤风险指标
                        ("dailyTrainingLoadAcute", "load_today"),
                        ("dailyAcuteChronicWorkloadRatio", "acwr_ratio"),
                    ]:
                        v = atl.get(src)
                        if v is not None:
                            fitness[dst] = round(v, 2) if isinstance(v, float) else v

        # 训练负荷平衡：monthlyLoadAerobicLow/High/Anaerobic + 目标区间 + 平衡反馈
        lb = (tr_status.get("mostRecentTrainingLoadBalance") or {}).get("metricsTrainingLoadBalanceDTOMap") or {}
        if isinstance(lb, dict) and lb:
            first_lb = next(iter(lb.values()), None)
            if isinstance(first_lb, dict):
                load_balance = {}
                for src, dst in [
                    ("monthlyLoadAerobicLow", "aerobic_low"),
                    ("monthlyLoadAerobicHigh", "aerobic_high"),
                    ("monthlyLoadAnaerobic", "anaerobic"),
                    ("monthlyLoadAerobicLowTargetMin", "aerobic_low_min"),
                    ("monthlyLoadAerobicLowTargetMax", "aerobic_low_max"),
                    ("monthlyLoadAerobicHighTargetMin", "aerobic_high_min"),
                    ("monthlyLoadAerobicHighTargetMax", "aerobic_high_max"),
                    ("monthlyLoadAnaerobicTargetMin", "anaerobic_min"),
                    ("monthlyLoadAnaerobicTargetMax", "anaerobic_max"),
                ]:
                    v = first_lb.get(src)
                    if v is not None:
                        load_balance[dst] = round(v, 1) if isinstance(v, float) else v
                phrase = first_lb.get("trainingBalanceFeedbackPhrase")
                if phrase:
                    load_balance["feedback"] = phrase
                if load_balance:
                    fitness["load_balance"] = load_balance

    race = _safe(g.get_race_predictions, default=None)
    if race and isinstance(race, dict):
        preds = {}
        for src, dst in [("time5K", "5k"), ("time10K", "10k"), ("timeHalfMarathon", "half"), ("timeMarathon", "full")]:
            secs = race.get(src)
            if secs:
                m, s = divmod(int(secs), 60)
                h, m = divmod(m, 60)
                preds[dst] = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
        if preds:
            fitness["race_predictions"] = preds

    if fitness:
        data["fitness"] = fitness

    # ── fitness_age（健身年龄，Garmin 官方"你到底几岁"评估）──
    fa = _safe(g.get_fitnessage_data, date_str, default=None)
    if fa and isinstance(fa, dict) and fa.get("fitnessAge"):
        if "fitness" not in data:
            data["fitness"] = {}
        # 简单 3 项 inline
        data["fitness"]["age"] = {
            "actual": fa.get("chronologicalAge"),
            "fitness": fa.get("fitnessAge"),
            "achievable": fa.get("achievableFitnessAge"),
        }
        # 需改善项：压成"key(current/target)"格式字符串，避免嵌套 list
        gaps_str = []
        gap_items = []
        for k, v in (fa.get("components") or {}).items():
            if isinstance(v, dict) and v.get("priority") is not None:
                val = v.get("value")
                tgt = v.get("targetValue")
                if val is not None and tgt is not None and val < tgt:
                    gap_items.append((v.get("priority"), k, val, tgt))
        gap_items.sort()
        for _, k, val, tgt in gap_items:
            gaps_str.append(f"{k}({val}/{tgt})")
        if gaps_str:
            data["fitness"]["age_gaps"] = ",".join(gaps_str)

    # ── vitals (呼吸率、血氧) ──
    vitals = {}
    resp = _safe(g.get_respiration_data, date_str, default=None)
    if resp and isinstance(resp, dict):
        avg = resp.get("avgWakingRespirationValue")
        lowest = resp.get("lowestRespirationValue")
        highest = resp.get("highestRespirationValue")
        if avg:
            vitals["respiration_avg"] = round(avg, 1)
        if lowest:
            vitals["respiration_min"] = round(lowest, 1)
        if highest:
            vitals["respiration_max"] = round(highest, 1)

    spo2 = _safe(g.get_spo2_data, date_str, default=None)
    if spo2 and isinstance(spo2, dict):
        avg = spo2.get("averageSpO2")
        lowest = spo2.get("lowestSpO2")
        if avg:
            vitals["spo2_avg"] = avg
        if lowest:
            vitals["spo2_min"] = lowest

    if vitals:
        data["vitals"] = vitals

    # ── activities ──
    acts_summary = _safe(g.get_activities_by_date, date_str, date_str, default=[]) or []
    activities = []
    for a in acts_summary:
        full = fetch_activity_full(g, a)
        activities.append(full)
    data["activities"] = activities

    return data


# ═══════════════════════════════════════════════
# 写 md（保留 weather + 追加人工笔记）
# ═══════════════════════════════════════════════

def write_health_md(data: dict) -> Path:
    date_str = data["date"]
    year_month = date_str[:7]
    out_dir = HEALTH_DIR / year_month
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{date_str}.md"

    # 保留旧 md 里的 weather 段和笔记段
    preserved_weather = None
    preserved_notes = None
    if out_path.exists():
        old_data = parse_frontmatter(out_path)
        if old_data.get("weather") and "weather" not in data:
            preserved_weather = old_data["weather"]
        old_text = out_path.read_text(encoding="utf-8")
        marker = "## 📝 我的笔记"
        if marker in old_text:
            preserved_notes = old_text[old_text.index(marker):]

    if preserved_weather:
        data["weather"] = preserved_weather

    fm = format_frontmatter(data)

    # 正文
    body_lines = [
        "",
        f"# 健康数据 · {date_str}（周{data.get('weekday','?')}）",
        "",
        "> 由 [[garmin_sync]] 自动同步。CLI 查询走 `garmin` 命令。",
        "",
    ]

    # 活动可读摘要
    acts = data.get("activities") or []
    if acts:
        body_lines.append("## 🏃 活动记录")
        for a in acts:
            typ_emoji = {"running": "🏃", "cycling": "🚴", "walking": "🚶", "strength_training": "💪"}.get(a.get("type"), "🏋️")
            loc = a.get("location") or "?"
            dist = a.get("distance_km") or 0
            dur = a.get("duration_min") or 0
            pace = a.get("avg_pace") or "?"
            avg_hr = a.get("avg_hr") or "?"
            max_hr = a.get("max_hr") or "?"
            time_str = a.get("time") or ""
            body_lines.append(f"### {typ_emoji} {loc} · {time_str}")
            body_lines.append(f"- **距离**: {dist} km  **时长**: {dur} 分钟  **均配**: {pace}")
            body_lines.append(f"- **心率**: 均 {avg_hr} / 峰 {max_hr}")
            te_a = a.get("training_effect_aerobic")
            te_an = a.get("training_effect_anaerobic")
            if te_a is not None or te_an is not None:
                body_lines.append(f"- **训练效果**: 有氧 {te_a} / 无氧 {te_an}")
            hrz = a.get("hr_zones") or {}
            z5 = hrz.get("z5_sec", 0)
            z4 = hrz.get("z4_sec", 0)
            total_hrz = sum(v for k, v in hrz.items() if k.endswith("_sec")) or 1
            if z5 / total_hrz > 0.5:
                body_lines.append(f"- ⚠️ **Zone 5 占比 {z5*100//total_hrz}%** — 强度偏高，注意恢复")
            elif z4 + z5 > 0:
                body_lines.append(f"- 心率区间：Z1-Z3 { (total_hrz - z4 - z5) // 60 } 分钟 · Z4 {z4//60} 分钟 · Z5 {z5//60} 分钟")
            splits = a.get("splits") or []
            if splits:
                body_lines.append("- **分段**：")
                for lap in splits:
                    if lap.get("dist_km", 0) < 0.5:
                        continue
                    body_lines.append(f"  - {lap.get('km')}km: 配 {lap.get('pace')}, 心率 {lap.get('hr')}")
            body_lines.append("")
    else:
        body_lines.append("## 🏃 活动记录")
        body_lines.append("- 无运动记录")
        body_lines.append("")

    if preserved_notes:
        body_lines.append(preserved_notes)
    else:
        body_lines.append("## 📝 我的笔记")
        body_lines.append("")
        body_lines.append("<!-- 想到啥写啥，Jarvis 会读 -->")
        body_lines.append("")

    out_path.write_text(fm + "\n" + "\n".join(body_lines), encoding="utf-8")
    print(f"✅ 写入: {out_path}")
    return out_path


def sync_day(date_str: str | None = None) -> Path:
    if date_str is None:
        yesterday = dt.date.today() - dt.timedelta(days=1)
        date_str = yesterday.isoformat()
    g = get_client()
    data = fetch_day(g, date_str)
    return write_health_md(data)


def backfill(days: int = 7) -> list[Path]:
    """一次拉最近 N 天（不含今天，含昨天）。"""
    paths = []
    g = get_client()
    for i in range(1, days + 1):
        d = (dt.date.today() - dt.timedelta(days=i)).isoformat()
        try:
            data = fetch_day(g, d)
            paths.append(write_health_md(data))
        except Exception as e:
            print(f"⚠️  {d} 挂了: {type(e).__name__}: {e}")
    return paths


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?", help="YYYY-MM-DD，默认昨天")
    ap.add_argument("--backfill", type=int, help="补拉最近 N 天")
    args = ap.parse_args()

    try:
        if args.backfill:
            paths = backfill(args.backfill)
            print(f"\n✅ 补拉完成 {len(paths)} 天")
        else:
            path = sync_day(args.date)
            print(f"\n✅ 完成: {path}")
    except RuntimeError as e:
        print(f"❌ {e}")
        sys.exit(1)
    except GarminConnectAuthenticationError as e:
        print(f"❌ Garmin 认证失败（token 可能过期）: {e}")
        print(f"   重跑: python src/init_garmin.py")
        sys.exit(2)
    except GarminConnectConnectionError as e:
        print(f"❌ 网络错误: {e}")
        sys.exit(3)
