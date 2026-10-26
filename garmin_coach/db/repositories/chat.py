"""chat_history：聊天记录读写与清理。"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
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


def add_chat(role: str, content: str, user_id: int = 0, msg_type: str = "text",
             audio_url: str | None = None, duration_sec: int | None = None,
             session_id: str | None = None):
    session = SessionLocal()
    try:
        session.add(ChatHistory(role=role, content=content, user_id=user_id,
                                session_id=session_id,
                                type=msg_type, audio_url=audio_url, duration_sec=duration_sec))
        session.commit()
    finally:
        session.close()


def get_chat_history(limit: int = 50, user_id: int = 0, date_str: str = "",
                     session_id: str | None = None) -> list[dict]:
    """取聊天历史。

    - ``session_id`` 给了就**只取该会话**的消息（会话恢复用）；
    - 不给则按日期取（前端历史列表用）。
    """
    session = SessionLocal()
    try:
        q = select(ChatHistory).where(ChatHistory.user_id == user_id)
        if session_id:
            q = q.where(ChatHistory.session_id == session_id)
        else:
            if not date_str:
                date_str = dt.date.today().isoformat()  # 默认今天
            q = q.where(ChatHistory.created_at.like(f"{date_str}%"))
        rows = session.execute(
            q.order_by(ChatHistory.id.desc()).limit(limit)
        ).scalars().all()
        return [{c.name: getattr(r, c.name) for c in ChatHistory.__table__.columns} for r in reversed(rows)]
    finally:
        session.close()


def clear_session_chat(session_id: str, user_id: int = 0) -> int:
    """删除某个会话的全部消息（评测隔离 / 用户主动清空单会话时用）。

    返回删除行数。注意：这是**物理删除**，会导致该会话无法再恢复。
    """
    session = SessionLocal()
    try:
        stmt = delete(ChatHistory).where(ChatHistory.session_id == session_id)
        if user_id:
            stmt = stmt.where(ChatHistory.user_id == user_id)
        result = session.execute(stmt)
        session.commit()
        return result.rowcount or 0
    except Exception:
        session.rollback()
        return 0
    finally:
        session.close()


def get_chat_dates(user_id: int = 0, days: int = 7) -> list[str]:
    """列出近 days 天有聊天记录的日期（供前端日期下拉框）。"""
    session = SessionLocal()
    try:
        cutoff = (dt.datetime.now() - dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        rows = session.execute(
            select(func.substr(ChatHistory.created_at, 1, 10))
            .where(ChatHistory.user_id == user_id, ChatHistory.created_at >= cutoff)
            .distinct()
            .order_by(func.substr(ChatHistory.created_at, 1, 10).desc())
        ).all()
        return [r[0] for r in rows]
    finally:
        session.close()


def cleanup_old_chat(days: int = 30):
    """删除 days 天前的聊天记录，含语音音频文件。"""
    session = SessionLocal()
    try:
        cutoff = (dt.datetime.now() - dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        # 1. 查旧语音消息的音频文件名
        rows = session.execute(
            select(ChatHistory.audio_url).where(
                ChatHistory.created_at < cutoff,
                ChatHistory.type == "voice",
                ChatHistory.audio_url.isnot(None),
            )
        ).all()
        # 2. 删除音频文件
        audio_dir = Path(__file__).resolve().parents[3] / "static" / "audio"
        for r in rows:
            url = r[0] or ""
            filename = url.rsplit("/", 1)[-1] if url else ""
            if filename:
                try:
                    (audio_dir / filename).unlink(missing_ok=True)
                except Exception:
                    pass
        # 3. 删除聊天记录
        session.execute(
            ChatHistory.__table__.delete().where(ChatHistory.created_at < cutoff)
        )
        session.commit()
    finally:
        session.close()


def clear_chat_history(user_id: int = 0):
    session = SessionLocal()
    try:
        session.execute(
            ChatHistory.__table__.delete().where(ChatHistory.user_id == user_id)
        )
        session.commit()
    finally:
        session.close()


# ═══════════════════════════════════════════════
# 手动训练记录
# ═══════════════════════════════════════════════


def get_sessions_of(date_str: str) -> list[dict]:
    """取某一天所有聊天过的会话（晚间记忆提炼用）。

    返回 ``[{session_id, user_id, messages}]``，按用户去重。
    """
    session = SessionLocal()
    try:
        day_start = f"{date_str} 00:00:00"
        day_end = f"{date_str} 23:59:59"
        rows = session.execute(
            select(
                ChatHistory.session_id,
                ChatHistory.user_id,
                func.count(ChatHistory.id),
            )
            .where(
                ChatHistory.created_at >= day_start,
                ChatHistory.created_at <= day_end,
                ChatHistory.session_id.isnot(None),
            )
            .group_by(ChatHistory.session_id, ChatHistory.user_id)
        ).all()
        return [
            {"session_id": r[0], "user_id": r[1], "messages": r[2]}
            for r in rows
            if r[0]
        ]
    finally:
        session.close()
