"""数据回放器 CLI（核心逻辑在 garmin_coach/services/demo_data.py）。

用法：
    python scripts/replay_data.py --list
    python scripts/replay_data.py --user 1001 --scenario good
    python scripts/replay_data.py --user 1001 --scenario fatigued --days 14
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from garmin_coach.services.demo_data import SCENARIOS, replay  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true", help="列出所有场景")
    parser.add_argument("--user", type=int, default=1001)
    parser.add_argument("--scenario", default="good")
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--memory", action="store_true", help="顺便写两条演示记忆")
    args = parser.parse_args()

    if args.list:
        for key, cfg in SCENARIOS.items():
            print(f"{key:10s} {cfg['title']}")
        return

    result = replay(args.user, args.scenario, args.days, args.seed, args.memory)
    print(f"✅ 已回放「{result['scenario']}」：{result['title']}")
    print(f"   user={args.user}  {result['days']} 天  天气列已保留")


if __name__ == "__main__":
    main()
