"""device_tokens：设备推送 token。"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Optional

from sqlalchemy import delete, func, inspect, select, text, update

from garmin_coach.db.models import (
    Activity,
    AgentRun,
    AgentStep,
    ChatHistory,
    DailyPlan,
    DeviceToken,
    ExerciseName,
    HealthDaily,
    MemoryOutbox,
    StrengthSet,
    User,
    UserMemory,
    WatchBinding,
)

from ._base import (
    SessionLocal,
    _insert_stmt,
    _upsert,
    _upsert_append,
)


def register_device_token(token: str, platform: str = "android") -> bool:
    session = SessionLocal()
    try:
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        session.execute(
            _upsert(
                DeviceToken,
                {"token": token, "platform": platform, "last_seen": now},
                ["token"],
                update_keys=["platform", "last_seen"],
            )
        )
        session.commit()
        return True
    except Exception:
        session.rollback()
        return False
    finally:
        session.close()


def get_active_tokens() -> list[str]:
    session = SessionLocal()
    try:
        cutoff = (dt.datetime.now() - dt.timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        rows = session.execute(
            select(DeviceToken.token).where(DeviceToken.last_seen > cutoff)
        ).all()
        return [r[0] for r in rows]
    finally:
        session.close()


def remove_token(token: str):
    session = SessionLocal()
    try:
        session.execute(DeviceToken.__table__.delete().where(DeviceToken.token == token))
        session.commit()
    finally:
        session.close()


# ═══════════════════════════════════════════════
# 推送提醒查询（供 push_service.check_and_notify 使用，保持原全局语义）
# ═══════════════════════════════════════════════
