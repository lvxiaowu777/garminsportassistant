"""安全档位的**可调阈值**（原来散落在 safety.py / safety_rules.py 里）。

改阈值只改这里；评估逻辑（`safety_rules.evaluate`）不写死数字。
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class SafetyThresholds:
    # ── HRV 基线 ──
    hrv_baseline_days: int = 30        # 用前多少天做基线（不含今天）
    hrv_min_samples: int = 7           # 基线至少要有几个样本
    hrv_rest_ratio: float = 0.80       # 低于基线 20% → REST
    hrv_easy_ratio: float = 0.90       # 低于基线 10% → EASY

    # ── 强度判定 ──
    hr_hard_pct: float = 0.85          # 达到最大心率 85% 且持续 → 算"上强度"
    hard_min_duration: int = 30        # 分
    long_duration: int = 75            # 单次超过这个时长也算上强度
    fallback_hard_hr: int = 155        # 没有年龄时退回绝对心率

    # ── ACWR（急慢性负荷比，来自手表）──
    acwr_rest: float = 1.8
    acwr_easy: float = 1.5
    acwr_low: float = 0.8

    # ── 睡眠 ──
    sleep_rest: float = 5.0
    sleep_easy: float = 6.0
    sleep_note: float = 7.0
    sleep_score_rest: int = 50
    sleep_score_easy: int = 60

    # ── 静息心率 / Body Battery / 准备度 ──
    resting_hr_easy: int = 70
    body_battery_easy: int = 30
    body_battery_note: int = 50
    readiness_rest_score: int = 30

    # ── 天气 ──
    heat_rest: float = 42.0
    heat_note: float = 38.0
    aqi_rest: int = 150
    aqi_easy: int = 100
    aqi_note: int = 75
    uv_note: int = 10
    rain_prob_note: float = 0.7

    # ── 昨天 ──
    yesterday_long_duration: int = 90

    # ── 无数据默认档（保守，原来是 LONG）──
    no_data_tier: str = "MODERATE"


def load_thresholds() -> SafetyThresholds:
    """允许用环境变量覆盖少数关键阈值（其余用默认值）。"""
    return SafetyThresholds(
        hrv_baseline_days=int(os.environ.get("SAFETY_HRV_BASELINE_DAYS", 30)),
        hrv_rest_ratio=float(os.environ.get("SAFETY_HRV_REST_RATIO", 0.80)),
        hrv_easy_ratio=float(os.environ.get("SAFETY_HRV_EASY_RATIO", 0.90)),
        hr_hard_pct=float(os.environ.get("SAFETY_HR_HARD_PCT", 0.85)),
        fallback_hard_hr=int(os.environ.get("SAFETY_FALLBACK_HARD_HR", 155)),
        no_data_tier=os.environ.get("SAFETY_NO_DATA_TIER", "MODERATE"),
    )


#: 全局阈值（和 settings 一样是模块级单例）
THRESHOLDS = load_thresholds()
