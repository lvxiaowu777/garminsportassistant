"""数据访问层（按表拆分后的聚合入口）。

对外仍然只暴露 `garmin_coach.db.database` 这一组函数，内部按表分模块：
users / watches / activities / health / chat / plans / push / _base。
"""
from __future__ import annotations

from ._base import *  # noqa: F401,F403
from .users import *  # noqa: F401,F403
from .watches import *  # noqa: F401,F403
from .activities import *  # noqa: F401,F403
from .health import *  # noqa: F401,F403
from .chat import *  # noqa: F401,F403
from .plans import *  # noqa: F401,F403
from .push import *  # noqa: F401,F403
from .safety import *  # noqa: F401,F403

from ._base import SessionLocal, engine, init_db  # noqa: F401

__all__ = [
    "SessionLocal",
    "add_chat",
    "bind_watch",
    "cleanup_old_chat",
    "clear_chat_history",
    "clear_session_chat",
    "create_user",
    "delete_activity",
    "engine",
    "get_active_brand",
    "get_active_tokens",
    "get_activity",
    "get_chat_dates",
    "get_chat_history",
    "get_dashboard",
    "get_day",
    "get_enabled_user_ids",
    "get_global_activity_count",
    "get_global_day_health",
    "get_last_sync_at",
    "get_last_sync_date",
    "get_latest_plan",
    "get_recent",
    "get_sessions_of",
    "get_standard_exercise_names",
    "get_user_by_phone",
    "get_user_profile",
    "get_watch_bindings",
    "get_watch_enabled",
    "get_weather",
    "init_db",
    "reset_db",
    "mark_synced",
    "record_training",
    "register_device_token",
    "remove_token",
    "save_plan",
    "save_safety_snapshot",
    "get_safety_snapshot",
    "seed_exercise_names",
    "select_watch",
    "set_watch_enabled",
    "unbind_watch",
    "update_activity_exercises",
    "update_last_sync_date",
    "update_user_profile",
    "upsert_health_daily",
]
