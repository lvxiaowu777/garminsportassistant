"""会话句柄：**只管 Session 的存在**，不碰 Agent、不跑对话、不做 TTL。

TTL 和回收在 ``agent/pool.py``：Agent 持有 Session，Agent 被淘汰时
``session.close()`` 一起收尾。这里剩下的只有：

- ``open()``：拿（或建）一个 Session 句柄；
- ``discard()``：从缓存里移除（Agent 淘汰时调）；
- ``drop()``：只清内存，不动库；
- ``reset()``：评测专用，内存 + 库 + 记忆一起清。
"""
from __future__ import annotations

import logging

from .store import Session, session_id_for

log = logging.getLogger(__name__)


class SessionManager:
    """Session 句柄缓存（同一天同一用户复用一个对象）。"""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    # ── 取用 ──

    def open(self, user_id: int, session_id: str | None = None) -> Session:
        """拿（或建）这个会话的句柄。"""
        sid = session_id or session_id_for(user_id)
        session = self._sessions.get(sid)
        if session is None:
            session = Session(session_id=sid, user_id=user_id)
            self._sessions[sid] = session
            log.info("session opened: %s", sid)
        return session

    def discard(self, session_id: str) -> None:
        """只从缓存里移除（Agent 池淘汰时用；close 由 Agent 负责）。"""
        self._sessions.pop(session_id, None)

    # ── 评测 / 测试 ──

    def drop(self, user_id: int) -> None:
        """只清内存（记录仍在库里，下次会 resume）。"""
        session = self._sessions.pop(session_id_for(user_id), None)
        if session is not None:
            session.closed = True    # 别让 drop 又投一次提炼任务

    def reset(self, user_id: int, *, wipe_messages: bool = True, wipe_memory: bool = False) -> dict:
        """**评测/测试专用**：内存和库一起清，让下一次请求从零开始。

        ``drop()`` 只清内存，下一次 ``resume()`` 会把库里同一天的消息再捞回来——
        评测每道题就会串味。``wipe_memory`` 再清长期记忆。
        生产环境不要调用。
        """
        sid = session_id_for(user_id)
        session = self._sessions.pop(sid, None)
        if session is not None:
            session.closed = True

        deleted = 0
        memory_deleted = 0
        if wipe_messages:
            try:
                from garmin_coach.db.database import clear_session_chat

                deleted = clear_session_chat(sid, user_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("reset session db failed: %s: %s", sid, exc)

        if wipe_memory:
            try:
                from sqlalchemy import delete

                from garmin_coach.db.database import SessionLocal
                from garmin_coach.db.models import MemoryOutbox, UserMemory

                with SessionLocal() as s:
                    memory_deleted = s.execute(
                        delete(UserMemory).where(UserMemory.user_id == user_id)
                    ).rowcount or 0
                    s.execute(delete(MemoryOutbox).where(MemoryOutbox.session_id == sid))
                    s.commit()
            except Exception as exc:  # noqa: BLE001
                log.warning("reset memory failed: %s: %s", sid, exc)

        log.info("session reset: %s (msgs=%d, memories=%d)", sid, deleted, memory_deleted)
        return {
            "session_id": sid,
            "removed_agent": session is not None,
            "deleted_messages": deleted,
            "deleted_memories": memory_deleted,
        }


#: 进程内单例（单 worker 足够；多 worker 时要换 Redis）
_manager: SessionManager | None = None


def get_session_manager() -> SessionManager:
    global _manager
    if _manager is None:
        _manager = SessionManager()
    return _manager
