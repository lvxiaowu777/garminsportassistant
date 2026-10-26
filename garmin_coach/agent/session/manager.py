"""会话生命周期：**只管 Session 的存在与回收**，不碰 Agent、不跑对话。

- ``open()/get()``：拿（或建）一个 Session 句柄；
- ``reap()``：空闲超时 / 数量超限的 Session 关闭并移出缓存；
- ``reset()``：评测专用，把内存和库一起清掉。

Agent 的缓存与回收在 ``agent/pool.py`` 里（Agent 持有 Session，不是反过来）。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Optional

from garmin_coach.settings import settings

from .store import Session, session_id_for

log = logging.getLogger(__name__)


class SessionManager:
    """Session 的缓存 + 回收。"""

    def __init__(
        self,
        ttl: int | None = None,
        max_sessions: int | None = None,
        *,
        on_close: "Callable[[Session], None] | None" = None,
    ) -> None:
        self._sessions: dict[str, Session] = {}
        self._ttl = ttl if ttl is not None else settings.session_ttl_seconds
        self._max = max_sessions if max_sessions is not None else settings.max_sessions
        self._on_close = on_close
        self._last_reap = time.time()

    # ── 取用 ──

    def open(self, user_id: int, session_id: str | None = None) -> Session:
        """拿（或建）这个会话的句柄。"""
        sid = session_id or session_id_for(user_id)
        session = self._sessions.get(sid)
        if session is None:
            session = Session(session_id=sid, user_id=user_id)
            self._sessions[sid] = session
            log.info("session opened: %s", sid)
        session.touch()
        return session

    def get(self, user_id: int) -> Session:
        """今天的会话。"""
        return self.open(user_id)

    def peek(self, user_id: int) -> Optional[Session]:
        return self._sessions.get(session_id_for(user_id))

    # ── 回收 ──

    def _maybe_reap(self) -> None:
        if time.time() - self._last_reap >= 60:
            self.reap()

    def reap(self) -> int:
        """关闭并移除空闲超时 / LRU 超限的会话；返回移除数量。"""
        self._last_reap = time.time()
        before = len(self._sessions)

        expired = [sid for sid, s in self._sessions.items() if s.idle_seconds > self._ttl]
        for sid in expired:
            self._remove(sid)

        if len(self._sessions) > self._max:
            ordered = sorted(self._sessions.items(), key=lambda kv: kv[1].last_active)
            for sid, _ in ordered[: len(self._sessions) - self._max]:
                self._remove(sid)

        removed = before - len(self._sessions)
        if removed:
            log.info("session reap: removed=%d alive=%d", removed, len(self._sessions))
        return removed

    def _remove(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return
        if self._on_close is not None:
            try:
                self._on_close(session)
            except Exception as exc:  # noqa: BLE001
                log.warning("on_close failed: %s: %s", session_id, exc)
        else:
            session.close()

    def discard(self, session_id: str) -> None:
        """只从缓存里移除，不触发 close（Agent 池淘汰时用）。"""
        self._sessions.pop(session_id, None)

    # ── 观测 ──

    def stats(self) -> dict:
        return {
            "alive": len(self._sessions),
            "sessions": [
                {
                    "session_id": s.session_id,
                    "user_id": s.user_id,
                    "idle_s": int(s.idle_seconds),
                }
                for s in self._sessions.values()
            ],
        }

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
