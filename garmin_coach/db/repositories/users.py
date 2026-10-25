"""users 表：注册、档案、同步时间戳。"""
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


def create_user(phone: str, password_hash: str) -> Optional[int]:
    """创建用户，成功返回 user_id，手机号重复返回 None。"""
    session = SessionLocal()
    try:
        exists = session.execute(
            select(User.id).where(User.phone == phone)
        ).first()
        if exists:
            return None
        user = User(phone=phone, password_hash=password_hash)
        session.add(user)
        session.commit()
        session.refresh(user)
        return user.id
    except Exception:
        session.rollback()
        return None
    finally:
        session.close()


def get_user_by_phone(phone: str) -> Optional[dict]:
    session = SessionLocal()
    try:
        row = session.execute(select(User).where(User.phone == phone)).scalars().first()
        return _orm_dict(row) if row else None
    finally:
        session.close()


_PROFILE_COLS = ("height_cm", "weight_kg", "age", "gender", "body_fat_pct",
                 "muscle_mass_kg", "goal", "goal_target", "training_years",
                 "training_freq_per_week", "training_types", "races")


def get_user_profile(user_id: int) -> dict:
    """获取用户档案（需求8字段 + 训练类型/比赛）。"""
    session = SessionLocal()
    try:
        row = session.execute(select(User).where(User.id == user_id)).scalars().first()
        if not row:
            return {}
        data = {c: getattr(row, c) for c in _PROFILE_COLS}
        # JSON 字段解码（数据库存的是 JSON 字符串，应用层用 list/dict）
        for key in ("training_types", "races"):
            raw = data.get(key)
            if raw:
                try:
                    data[key] = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    data[key] = []
            else:
                data[key] = []
        return data
    finally:
        session.close()


def update_user_profile(user_id: int, data: dict) -> bool:
    """更新用户档案（只接受允许的字段，忽略 None）。"""
    values = {}
    for k, v in data.items():
        if k not in _PROFILE_COLS or v is None:
            continue
        # JSON 字段需要序列化
        if k in ("training_types", "races"):
            if isinstance(v, (list, dict)):
                values[k] = json.dumps(v, ensure_ascii=False)
            else:
                values[k] = v
        else:
            values[k] = v
    if not values:
        return False
    session = SessionLocal()
    try:
        session.execute(User.__table__.update().where(User.id == user_id).values(**values))
        session.commit()
        return True
    finally:
        session.close()


def get_last_sync_date(user_id: int) -> str:
    """返回用户上次同步日期（YYYY-MM-DD），无则空串。"""
    session = SessionLocal()
    try:
        row = session.execute(select(User.last_sync_date).where(User.id == user_id)).first()
        return row[0] if row and row[0] else ""
    finally:
        session.close()


def update_last_sync_date(user_id: int, date_str: str):
    """更新用户上次同步日期。"""
    session = SessionLocal()
    try:
        session.execute(User.__table__.update().where(User.id == user_id).values(last_sync_date=date_str))
        session.commit()
    finally:
        session.close()


def mark_synced(user_id: int) -> None:
    """标记这个用户刚同步过（对话前判断新鲜度用）。"""
    session = SessionLocal()
    try:
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # 用 UPDATE 而不是 upsert：users 有 NOT NULL 列（phone / password_hash），
        # 只带 id 的 INSERT 会失败
        session.execute(
            update(User)
            .where(User.id == user_id)
            .values(last_sync_at=now, last_sync_date=now[:10])
        )
        session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()


def get_last_sync_at(user_id: int) -> str:
    """上次同步时间（'YYYY-MM-DD HH:MM:SS'），没有则空字符串。"""
    session = SessionLocal()
    try:
        row = session.execute(
            select(User.last_sync_at).where(User.id == user_id)
        ).first()
        return (row[0] or "") if row else ""
    except Exception:
        return ""
    finally:
        session.close()


def _orm_dict(obj) -> dict:
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


# ── 手表绑定 ──
