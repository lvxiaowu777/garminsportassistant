"""过渡层：把还在 ``src/`` 下的旧模块接进新包。

``database.py`` / ``models.py`` **已经搬进** ``garmin_coach/db/``，所以这个文件
现在只剩一个职责：**把 ``src/`` 放进 ``sys.path``**，让 ``auth`` / ``garmin_sync`` /
``garmin_server_sync`` / ``push_service`` / ``weather_server`` 这些还没搬的模块能被 import。

等这些模块也迁完，这个文件和 ``api/deps.py`` 里的 import 一起删。

另外它继续 re-export 常用查询函数，路由/工具层可以只 import 这里，
将来再改数据层时调用点不用动。
"""
from __future__ import annotations

import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from garmin_coach.db.database import (  # noqa: E402
    get_activity,
    get_dashboard,
    get_day,
    get_latest_plan,
    get_recent,
    get_standard_exercise_names,
    get_user_profile,
    get_weather,
    record_training,
    save_plan,
)

__all__ = [
    "get_activity",
    "get_dashboard",
    "get_day",
    "get_latest_plan",
    "get_recent",
    "get_standard_exercise_names",
    "get_user_profile",
    "get_weather",
    "record_training",
    "save_plan",
]
