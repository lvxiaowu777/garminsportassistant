"""safety 规则自测：不连数据库，只测纯函数和取数辅助。

跑法：python scripts/safety_selftest.py
"""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from garmin_coach.services import safety_config  # noqa: E402
from garmin_coach.services.safety import estimate_hrmax, hrv_baseline_of  # noqa: E402
from garmin_coach.services.safety_rules import evaluate  # noqa: E402

TODAY = "2026-10-28"
PASS = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS
    if not cond:
        raise AssertionError(f"{name} 失败 {detail}")
    PASS += 1
    print(f"✓ {name}")


def main() -> None:
    # 1) HRV 基线：排除今天、用中位数、样本不足返回 None
    recent = [
        {"date": TODAY, "hrv": 30},                       # 今天异常低，不能进基线
        *[{"date": f"2026-10-{d:02d}", "hrv": v} for d, v in
          [(27, 60), (26, 62), (25, 58), (24, 61), (23, 59), (22, 63), (21, 57)]],
    ]
    baseline = hrv_baseline_of(recent, TODAY)
    check("基线排除今天且取中位数", baseline == 60.0, f"got {baseline}")

    few = [{"date": TODAY, "hrv": 50}, {"date": "2026-10-27", "hrv": 60}]
    check("样本不足返回 None", hrv_baseline_of(few, TODAY) is None)

    # 2) HRV 低于基线 20% → REST；低 10% → EASY
    data = {"hrv": 47, "sleep_hours": 8, "resting_hr": 55}
    tier, blockers, _ = evaluate(data, hrv_baseline=60.0, hard_hr=170)
    check("HRV 低 20% 降到 REST", tier == "REST", f"got {tier}, {blockers}")

    data = {"hrv": 52, "sleep_hours": 8, "resting_hr": 55}
    tier, blockers, _ = evaluate(data, hrv_baseline=60.0, hard_hr=170)
    check("HRV 低 10% 降到 EASY", tier == "EASY", f"got {tier}")

    data = {"hrv": 60, "sleep_hours": 8, "resting_hr": 55}
    tier, _, _ = evaluate(data, hrv_baseline=60.0, hard_hr=170)
    check("HRV 正常不降档", tier == "LONG", f"got {tier}")

    # 3) %HRmax：同样的心率，年轻人算轻松、年长者算强度
    yday = [{"type": "running", "dur_min": 40, "avg_hr": 150}]
    tier_young, _, _ = evaluate(
        {"yesterday_activities": yday, "hrv": 60}, hard_hr=170
    )
    check("150bpm 对 HRmax 200 不算强度", tier_young == "LONG", f"got {tier_young}")

    tier_old, _, _ = evaluate(
        {"yesterday_activities": yday, "hrv": 60}, hard_hr=127
    )
    check("150bpm 对 HRmax 150 算强度", tier_old == "EASY", f"got {tier_old}")

    # 4) Tanaka 公式
    check("hrmax(30)=187", estimate_hrmax(30) == 187, str(estimate_hrmax(30)))
    check("无年龄返回 None", estimate_hrmax(None) is None)

    # 5) 阈值可配置：临时把 ACWR 门槛调低
    old = safety_config.THRESHOLDS
    safety_config.THRESHOLDS = dataclasses.replace(old, acwr_rest=1.2, acwr_easy=1.1)
    try:
        tier, _, _ = evaluate({"acwr_ratio": 1.3, "hrv": 60})
        check("阈值可配置（ACWR 1.2 就 REST）", tier == "REST", f"got {tier}")
    finally:
        safety_config.THRESHOLDS = old

    print(f"\n全部通过：{PASS} 项")


if __name__ == "__main__":
    main()
