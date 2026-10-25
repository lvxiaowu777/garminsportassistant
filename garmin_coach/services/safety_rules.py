"""
安全规则 —— Claude 判训练强度前，硬规则先跑一遍。

设计理念（对齐用户"挑战自己但安全为主"）：
- 规则决定"能不能上强度"（是/否 + 上限档位）
- 大模型决定"具体怎么练"（在允许范围内出方案）
- 规则永远优于大模型。任何一条硬闸拦下，大模型必须收敛。

档位定义：
- REST     : 完全休息或走路 <30min
- EASY     : 轻松跑（心率 <140），≤40min
- MODERATE : 有氧稳态（140~155），≤60min
- HARD     : 阈值/间歇（155+），≤50min
- LONG     : 长距离慢跑（<150），60~120min
"""

from typing import Dict, List, Tuple

TIER_ORDER = ["REST", "EASY", "MODERATE", "HARD", "LONG"]


def evaluate(data: Dict, recent_hrv_avg: float | None = None,
             recent_hard_days: int = 0) -> Tuple[str, List[str], List[str]]:
    """
    根据当日数据 + 最近趋势，返回 (最高允许档位, 硬闸原因列表, 提醒列表)。

    参数：
        data              : 当日健康数据（garmin_sync 落的 dict）
        recent_hrv_avg    : 最近 7 天 HRV 均值，用于对比今日
        recent_hard_days  : 最近 3 天里有几天上了强度（HARD/LONG）

    返回：
        max_tier    : "REST" | "EASY" | "MODERATE" | "HARD" | "LONG"
        blockers    : 触发降档的硬理由（决策级）
        notes       : 需要注意但不强制的信号
    """
    blockers: List[str] = []
    notes: List[str] = []
    max_idx = len(TIER_ORDER) - 1  # 默认 LONG，逐个降

    def cap(tier: str, reason: str):
        nonlocal max_idx
        i = TIER_ORDER.index(tier)
        if i < max_idx:
            max_idx = i
            blockers.append(f"{reason} → 上限 {tier}")

    # ── HRV（恢复的核心指标）──
    hrv = data.get("hrv")
    if hrv is not None and recent_hrv_avg:
        if hrv < recent_hrv_avg * 0.8:
            cap("REST", f"HRV {hrv} 比近 7 日均值 {recent_hrv_avg:.0f} 低 20%+")
        elif hrv < recent_hrv_avg * 0.9:
            cap("EASY", f"HRV {hrv} 比近 7 日均值 {recent_hrv_avg:.0f} 低 10%+")

    hrv_status = data.get("hrv_status")
    if hrv_status in ("POOR", "LOW"):
        cap("EASY", f"HRV 状态为 {hrv_status}")
    elif hrv_status == "UNBALANCED":
        notes.append(f"HRV 状态 UNBALANCED，注意监控")

    # ── 手表官方训练状态（Garmin ACWR + 训练反馈）──
    acwr = data.get("acwr_ratio")
    if acwr is not None:
        if acwr >= 1.8:
            cap("REST", f"急慢性负荷比 {acwr}（≥1.8 严重超标，受伤风险很高）")
            blockers.append(f"⚠️ ACWR={acwr} 受伤风险区，建议休息几天让 chronic load 追上来")
        elif acwr >= 1.5:
            cap("EASY", f"急慢性负荷比 {acwr}（≥1.5 受伤风险高）")
        elif acwr < 0.8:
            notes.append(f"急慢性负荷比 {acwr} 偏低，说明训练量不足")

    tr_feedback = data.get("training_feedback") or ""
    if "OVERREACHING" in tr_feedback:
        cap("EASY", f"手表提示 {tr_feedback}（过度训练预警）")
    elif "UNPRODUCTIVE" in tr_feedback:
        notes.append(f"手表提示 {tr_feedback}（训练无效，可能需调整强度分布）")
    elif "STRAINED" in tr_feedback:
        cap("REST", f"手表提示 {tr_feedback}（身体紧张，需恢复）")

    # AEROBIC_LOW_SHORTAGE：缺低强度慢跑（结构性问题，长期提醒）
    load_balance = data.get("load_balance") or {}
    lb_feedback = load_balance.get("feedback") if isinstance(load_balance, dict) else None
    if lb_feedback == "AEROBIC_LOW_SHORTAGE":
        notes.append("月度负荷失衡：缺低强度有氧（慢跑心率 <140），需要补几次真正的慢跑打有氧基础")

    # ── 睡眠 ──
    sleep_h = data.get("sleep_hours")
    sleep_s = data.get("sleep_score")
    if sleep_h is not None:
        if sleep_h < 5:
            cap("REST", f"睡眠仅 {sleep_h}h")
        elif sleep_h < 6:
            cap("EASY", f"睡眠 {sleep_h}h 偏少")
        elif sleep_h < 7:
            notes.append(f"睡眠 {sleep_h}h 一般，别太拉")
    if sleep_s is not None:
        if sleep_s < 50:
            cap("REST", f"睡眠得分 {sleep_s} 很差")
        elif sleep_s < 60:
            cap("EASY", f"睡眠得分 {sleep_s} 偏低")

    # ── 静息心率 ──
    rhr = data.get("resting_hr")
    if rhr is not None and rhr > 70:
        cap("EASY", f"静息心率 {rhr} 偏高（可能疲劳/病）")

    # ── Body Battery（早晨读数）──
    bb_high = data.get("body_battery_high")
    if bb_high is not None:
        if bb_high < 30:
            cap("EASY", f"Body Battery 早晨最高 {bb_high} 偏低")
        elif bb_high < 50:
            notes.append(f"Body Battery 早晨 {bb_high}，谨慎上强度")

    # ── 训练准备度 ──
    tr_level = data.get("training_readiness_level")
    tr_score = data.get("training_readiness")
    if tr_level in ("LOW", "POOR"):
        cap("EASY", f"训练准备度 {tr_level}")
    if tr_score is not None and tr_score < 30:
        cap("REST", f"训练准备度分数 {tr_score} 过低")

    # ── 连续高强度 ──
    if recent_hard_days >= 3:
        cap("EASY", f"最近 3 天已上过 {recent_hard_days} 次强度，该恢复了")
    elif recent_hard_days >= 2:
        cap("MODERATE", f"最近 3 天上过 {recent_hard_days} 次强度")

    # ── 天气（热应激 + 空气）──
    w = data.get("weather") or {}
    high_temp = w.get("high")
    humidity = w.get("humidity")
    if high_temp is not None and humidity is not None:
        heat_index = high_temp + humidity * 0.1
        if heat_index >= 42:  # 32°C + 100% 湿度 ≈ 42
            cap("EASY", f"高温高湿（{high_temp}°C / {humidity}%），户外强度危险")
            notes.append("建议早晨或晚上跑，或改室内")
        elif heat_index >= 38:
            notes.append(f"天气偏热（{high_temp}°C / 湿度{humidity}%），避开正午")

    rain_prob = w.get("rain_prob")
    if rain_prob is not None and rain_prob > 0.7:
        notes.append(f"降雨概率 {int(rain_prob*100)}%，备好雨天预案（室内替代）")

    # 空气质量（和风 AQI）
    aqi = w.get("aqi")
    if aqi is not None:
        if aqi >= 150:
            cap("REST", f"AQI {aqi}（{w.get('aqi_category', '不健康')}），户外运动不建议")
        elif aqi >= 100:
            cap("EASY", f"AQI {aqi}（{w.get('aqi_category', '轻度污染')}），只做低强度")
            notes.append("建议室内或戴口罩")
        elif aqi >= 75:
            notes.append(f"AQI {aqi}，敏感人群留意")

    # UV 强度
    uv = w.get("uv_index")
    if uv is not None and uv >= 10:
        notes.append(f"UV 指数 {uv}（极强），避开 10:00-16:00，务必防晒")

    # ── 昨天已经练过强度？ ──
    acts = data.get("yesterday_activities") or []
    for a in acts:
        dur = a.get("dur_min") or 0
        hr = a.get("avg_hr") or 0
        if hr >= 155 and dur >= 30:
            cap("EASY", f"昨天已上强度（心率{hr}/{dur}min），今天恢复")
            break
        if dur >= 90:
            cap("EASY", f"昨天长距离（{dur}min），今天恢复")
            break

    return TIER_ORDER[max_idx], blockers, notes


def tier_description(tier: str) -> str:
    return {
        "REST": "完全休息或走路 <30min",
        "EASY": "轻松跑（心率 <140），≤40min",
        "MODERATE": "有氧稳态（心率 140~155），≤60min",
        "HARD": "阈值/间歇（心率 155+），≤50min",
        "LONG": "长距离慢跑（心率 <150），60~120min",
    }.get(tier, tier)
