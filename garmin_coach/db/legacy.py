"""过渡层：把还在 ``src/`` 下的旧模块接进新包。

这是绞杀者模式的临时脚手架。``database.py`` 等模块整体搬进
``garmin_coach/db/`` 之后，这个文件就该删掉。

为什么要单独一层：新包不该到处写 ``sys.path.insert``，集中在这里做一次，
其他模块只 ``from garmin_coach.db.legacy import get_day`` 就行，
将来搬迁时只需要改这一个文件。
"""
from __future__ import annotations

import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from database import (  # noqa: E402
    get_activity,
    get_dashboard,
    get_day,
    get_latest_plan,
    get_recent,
    get_standard_exercise_names,
    get_user_profile,
    get_weather,
    has_health_data,
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
    "has_health_data",
    "record_training",
    "save_plan",
]
