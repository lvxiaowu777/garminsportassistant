"""watch_bindings 表：绑定、解绑、启用开关、当前品牌。"""
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

from .users import _orm_dict


def get_active_brand(user_id: int = 0) -> str:
    """当前启用的手表品牌（仪表盘数据源）。返回第一个 enabled=1 的绑定，无则默认 garmin。"""
    session = SessionLocal()
    try:
        row = session.execute(
            select(WatchBinding.brand)
            .where(WatchBinding.user_id == user_id, WatchBinding.enabled == 1)
            .order_by(WatchBinding.id)
            .limit(1)
        ).first()
        return row[0] if row else "garmin"
    finally:
        session.close()


def bind_watch(user_id: int, brand: str, account: str, auth_token: str = "", token_expires: str = "") -> bool:
    """绑定手表品牌。已存在则更新 token。"""
    session = SessionLocal()
    try:
        existing = session.execute(
            select(WatchBinding).where(
                WatchBinding.user_id == user_id, WatchBinding.brand == brand
            )
        ).scalars().first()
        if existing:
            existing.account = account
            existing.auth_token = auth_token
            existing.token_expires = token_expires
        else:
            session.add(WatchBinding(
                user_id=user_id, brand=brand, account=account,
                auth_token=auth_token, token_expires=token_expires,
            ))
        session.commit()
        return True
    except Exception:
        session.rollback()
        return False
    finally:
        session.close()


def unbind_watch(user_id: int, brand: str) -> bool:
    session = SessionLocal()
    try:
        result = session.execute(
            WatchBinding.__table__.delete().where(
                WatchBinding.user_id == user_id, WatchBinding.brand == brand
            )
        )
        session.commit()
        return result.rowcount > 0
    finally:
        session.close()


def get_watch_bindings(user_id: int) -> list[dict]:
    session = SessionLocal()
    try:
        rows = session.execute(
            select(WatchBinding.id, WatchBinding.brand, WatchBinding.account,
                   WatchBinding.enabled, WatchBinding.created_at)
            .where(WatchBinding.user_id == user_id)
        ).all()
        return [dict(r._mapping) for r in rows]
    finally:
        session.close()


def set_watch_enabled(user_id: int, brand: str, enabled: bool) -> bool:
    session = SessionLocal()
    try:
        result = session.execute(
            WatchBinding.__table__.update()
            .where(WatchBinding.user_id == user_id, WatchBinding.brand == brand)
            .values(enabled=1 if enabled else 0)
        )
        session.commit()
        return result.rowcount > 0
    finally:
        session.close()


def select_watch(user_id: int, brand: str) -> bool:
    """单选设备：把 brand 设为 enabled=1，其他品牌设为 enabled=0。"""
    session = SessionLocal()
    try:
        exists = session.execute(
            select(WatchBinding.id).where(
                WatchBinding.user_id == user_id, WatchBinding.brand == brand
            )
        ).first()
        if not exists:
            return False
        session.execute(
            WatchBinding.__table__.update().where(WatchBinding.user_id == user_id).values(enabled=0)
        )
        session.execute(
            WatchBinding.__table__.update().where(
                WatchBinding.user_id == user_id, WatchBinding.brand == brand
            ).values(enabled=1)
        )
        session.commit()
        return True
    finally:
        session.close()


# ── 手表同步辅助（供 garmin_server_sync 使用；任何品牌的同步器都用这两个） ──


def get_watch_enabled(user_id: int, brand: str) -> bool:
    """查某用户某品牌是否启用。未绑定时返回 True（视为不拦截）。"""
    session = SessionLocal()
    try:
        row = session.execute(
            select(WatchBinding.enabled).where(
                WatchBinding.user_id == user_id, WatchBinding.brand == brand
            )
        ).first()
        if row is None:
            return True
        return row[0] == 1
    finally:
        session.close()


def get_enabled_user_ids(brand: str) -> list[int]:
    """查所有启用了某品牌的 user_id（定时同步用）。"""
    session = SessionLocal()
    try:
        rows = session.execute(
            select(WatchBinding.user_id).where(
                WatchBinding.brand == brand, WatchBinding.enabled == 1
            ).distinct()
        ).all()
        return [r[0] for r in rows]
    finally:
        session.close()


# ═══════════════════════════════════════════════
# 设备推送 Token
# ═══════════════════════════════════════════════
