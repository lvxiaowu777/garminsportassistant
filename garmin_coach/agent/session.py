"""会话运行时：**一个会话 = 一个 Agent 实例**。

三层职责，别混：

- ``ConversationManager``（conversation.py）：只存消息，不碰库、不知道 TTL；
- ``Session`` / ``SessionManager``（本文件）：会话的身份、生命周期、并发锁、
  **落库与恢复**；
- ``Agent``（loop.py）：拿 CM 跑一轮决策循环。

持久化通过注入的两个回调完成（``loader`` / ``persister``），
这样本模块不直接依赖 ``src/database.py``，也方便测试时替换。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from garmin_coach.llm.config import get_provider
from garmin_coach.llm.factory import get_shared_client
from garmin_coach.settings import settings
from garmin_coach.tools.health import build_default_registry

from .loop import Agent, AgentResult

log = logging.getLogger(__name__)

#: 会话恢复：给定 (user_id, session_id) 返回该会话的历史消息
HistoryLoader = Callable[[int, str], list[dict]]
#: 单条消息落库：(session_id, user_id, role, content) -> None
MessagePersister = Callable[[str, int, str, str], None]
#: 运行轨迹落库（关键字参数，见 services/trace.py: save_run）
TraceRecorder = Callable[..., Any]
#: 会话结束钩子（默认投递记忆提炼任务）
CloseHook = Callable[[int, str], None]

#: Agent 空闲多久回收（秒）。回收 ≠ 结束会话：记录还在库里，下次可恢复。
#: 值来自 ``garmin_coach.settings``（环境变量 ``SESSION_TTL_SECONDS``）。
SESSION_TTL = settings.session_ttl_seconds
#: 同时驻留内存的会话上限（LRU 淘汰）。环境变量 ``MAX_SESSIONS``。
MAX_SESSIONS = settings.max_sessions


def _default_loader(user_id: int, session_id: str) -> list[dict]:
    """从 MySQL 读该会话的历史（延迟导入，避免 agent 层依赖 database 的导入顺序）。"""
    from database import get_chat_history

    return get_chat_history(50, user_id, session_id=session_id)


def _default_persister(session_id: str, user_id: int, role: str, content: str) -> None:
    from database import add_chat

    add_chat(role, content, user_id, session_id=session_id)


def _default_on_close(user_id: int, session_id: str) -> None:
    """会话回收 → 投一条长期记忆提炼待办（离线消费）。"""
    from garmin_coach.services.memory import enqueue_session

    enqueue_session(user_id, session_id)


def _default_tracer(**kwargs: Any) -> None:
    """把一次 run 的轨迹写进 agent_runs / agent_steps。"""
    from garmin_coach.services.trace import save_run

    save_run(**kwargs)


def session_id_for(user_id: int, day: dt.date | None = None) -> str:
    """会话 id 规则：一天一个 —— ``u1001-2026-10-25``。"""
    return f"u{user_id}-{(day or dt.date.today()).isoformat()}"


@dataclass
class Session:
    """一个会话的内存态。"""

    session_id: str
    user_id: int
    agent: Agent
    provider: str = ""
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    turns: int = 0

    def touch(self) -> None:
        self.last_active = time.time()

    @property
    def idle_seconds(self) -> float:
        return time.time() - self.last_active


class SessionManager:
    """管住所有会话：取用、串行化、回收。"""

    def __init__(
        self,
        ttl: int | None = None,
        max_sessions: int | None = None,
        *,
        loader: HistoryLoader | None = None,
        persister: MessagePersister | None = None,
        on_close: CloseHook | None = None,
        tracer: TraceRecorder | None = None,
    ) -> None:
        self._sessions: dict[str, Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._ttl = ttl if ttl is not None else settings.session_ttl_seconds
        self._max = max_sessions if max_sessions is not None else settings.max_sessions
        self._loader = loader
        self._persister = persister
        self._on_close = on_close
        self._tracer = tracer
        self._last_reap = time.time()
        self.restored = 0  # 启动以来从库里恢复过多少次（观测用）

    # ── 取用 ──

    def get(
        self,
        user_id: int,
        *,
        provider_name: str | None = None,
        max_turns: int | None = None,
    ) -> Session:
        """拿这个用户今天的会话；没有就建一个（建的时候从库里恢复历史）。"""
        self._maybe_reap()
        sid = session_id_for(user_id)
        session = self._sessions.get(sid)

        if session is None:
            session = self._build(user_id, sid, provider_name, max_turns)
            self._restore(session)
            self._sessions[sid] = session
            log.info(
                "session created: %s (provider=%s, restored=%d msgs)",
                sid, session.provider, len(session.agent.conversation),
            )
        elif provider_name and session.provider and session.provider != provider_name:
            # 同一个会话换了 provider：重建 Agent，历史保留
            old_messages = session.agent.conversation.messages
            session = self._build(user_id, sid, provider_name, max_turns)
            session.agent.conversation.messages.extend(old_messages)
            self._sessions[sid] = session
            log.info("session rebuilt for provider switch: %s -> %s", sid, provider_name)

        session.touch()
        return session

    def _build(
        self,
        user_id: int,
        session_id: str,
        provider_name: str | None,
        max_turns: int | None,
    ) -> Session:
        provider = get_provider(provider_name)
        if not provider.api_key:
            raise RuntimeError(f"{provider.display_name} 的 API key 未配置")

        agent = Agent(
            get_shared_client(provider),          # 传输层：所有会话共享
            build_default_registry(user_id),      # 工具层：按 user 绑定
            user_id=user_id,
            session_id=session_id,
            max_turns=max_turns if max_turns is not None else settings.agent_max_turns,
            temperature=settings.agent_temperature,
            max_tokens=provider.max_tokens,
        )
        return Session(
            session_id=session_id,
            user_id=user_id,
            agent=agent,
            provider=provider.key,
        )

    # ── 恢复 ──

    def _restore(self, session: Session) -> None:
        """从库里把该会话的历史灌进 CM。失败不影响对话（只是没有历史）。"""
        if self._loader is None:
            return
        try:
            history = self._loader(session.user_id, session.session_id) or []
        except Exception as exc:  # noqa: BLE001 - 恢复失败必须让对话继续
            log.warning("session restore failed: %s: %s", session.session_id, exc)
            return
        if history:
            session.agent.conversation.load_history(history)
            self.restored += 1

    # ── 跑一轮 ──

    async def stream_turn(
        self,
        user_id: int,
        message: str,
        *,
        hydrate: list[dict] | None = None,
        injected: list[dict] | None = None,
        provider_name: str | None = None,
        max_turns: int | None = None,
    ) -> AsyncIterator[Any]:
        """跑一轮并**逐事件产出**（SSE 用），结束时落库 + 记 trace。

        ``run_turn`` 就是把这一个生成器消费完，两者共用同一套副作用，
        所以不会出现「流式走了、聊天历史没存」的问题。
        """
        from .events import DoneEvent

        session = self.get(
            user_id,
            provider_name=provider_name,
            max_turns=max_turns if max_turns is not None else settings.agent_max_turns,
        )
        lock = self._locks.setdefault(session.session_id, asyncio.Lock())
        async with lock:
            # 先落 user：即使模型/网络中途挂了，用户说的话也不会丢
            self._persist_message(session, "user", message)
            t0 = time.perf_counter()
            result: AgentResult | None = None
            try:
                async for event in session.agent.run(
                    message, hydrate=hydrate, injected=injected
                ):
                    if isinstance(event, DoneEvent):
                        result = event.result
                    yield event
            finally:
                session.turns += 1
                session.touch()
                elapsed = int((time.perf_counter() - t0) * 1000)
                if result is not None:
                    self._persist_message(session, "assistant", result.text)
                    self._record_trace(session, result, elapsed)

    async def run_turn(
        self,
        user_id: int,
        message: str,
        *,
        hydrate: list[dict] | None = None,
        injected: list[dict] | None = None,
        provider_name: str | None = None,
        max_turns: int | None = None,
        on_text: Any = None,
    ) -> AgentResult:
        """跑一轮并返回最终结果（非流式接口用）。"""
        from .events import DoneEvent, TextEvent

        result: AgentResult | None = None
        async for event in self.stream_turn(
            user_id,
            message,
            hydrate=hydrate,
            injected=injected,
            provider_name=provider_name,
            max_turns=max_turns,
        ):
            if isinstance(event, TextEvent) and on_text is not None:
                on_text(event.text)
            elif isinstance(event, DoneEvent):
                result = event.result
        return result or AgentResult(text="", turns=0)

    def _record_trace(self, session: Session, result: AgentResult, elapsed_ms: int) -> None:
        """写一次 run 的轨迹。失败只记日志。"""
        if self._tracer is None:
            return
        try:
            self._tracer(
                user_id=session.user_id,
                session_id=session.session_id,
                provider=session.provider,
                turns=result.turns,
                total_tokens=int(result.usage.get("total_tokens", 0)),
                latency_ms=elapsed_ms,
                error=result.error,
                steps=result.steps,
                tool_calls=result.tool_calls,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("trace record failed: %s: %s", session.session_id, exc)

    def _persist_message(self, session: Session, role: str, content: str) -> None:
        """单条落库。失败只记日志，不影响已经发生的事实。"""
        if self._persister is None:
            return
        try:
            self._persister(session.session_id, session.user_id, role, content)
        except Exception as exc:  # noqa: BLE001
            log.warning("persist %s failed: %s: %s", role, session.session_id, exc)

    # ── 回收 ──

    def _maybe_reap(self) -> None:
        if time.time() - self._last_reap >= 60:
            self.reap()

    def reap(self) -> int:
        """回收空闲超时的会话 + LRU 超限；返回回收数量。"""
        self._last_reap = time.time()
        before = len(self._sessions)

        expired = [sid for sid, s in self._sessions.items() if s.idle_seconds > self._ttl]
        for sid in expired:
            session = self._sessions.pop(sid, None)
            self._locks.pop(sid, None)
            if session is not None:
                self._notify_closed(session)

        if len(self._sessions) > self._max:
            ordered = sorted(self._sessions.items(), key=lambda kv: kv[1].last_active)
            for sid, session in ordered[: len(self._sessions) - self._max]:
                self._sessions.pop(sid, None)
                self._locks.pop(sid, None)
                self._notify_closed(session)

        removed = before - len(self._sessions)
        if removed:
            log.info("session reap: removed=%d alive=%d", removed, len(self._sessions))
        return removed

    # ── 观测 ──

    def _notify_closed(self, session: Session) -> None:
        """会话被回收时通知外部（默认：投一条记忆提炼待办）。"""
        if self._on_close is None:
            return
        try:
            self._on_close(session.user_id, session.session_id)
        except Exception as exc:  # noqa: BLE001 - 回收流程不能因为钩子失败而中断
            log.warning("on_close failed: %s: %s", session.session_id, exc)

    def stats(self) -> dict:
        return {
            "alive": len(self._sessions),
            "restored": self.restored,
            "sessions": [
                {
                    "session_id": s.session_id,
                    "user_id": s.user_id,
                    "provider": s.provider,
                    "turns": s.turns,
                    "messages": len(s.agent.conversation),
                    "idle_s": int(s.idle_seconds),
                }
                for s in self._sessions.values()
            ],
        }

    def drop(self, user_id: int) -> None:
        """只清内存（记录仍保留在库里，下次会恢复）。"""
        sid = session_id_for(user_id)
        self._sessions.pop(sid, None)
        self._locks.pop(sid, None)

    def reset(self, user_id: int, *, wipe_messages: bool = True, wipe_memory: bool = False) -> dict:
        """**评测/测试专用**：内存和库一起清，让下一次请求从零开始。

        为什么需要它：``drop()`` 只清内存，下一次请求的 ``_restore()`` 会把
        库里同一天的消息再捞回来 —— 评测每道题就会串味。
        ``wipe_memory`` 再清长期记忆（评测要求每题完全独立时开）。
        生产环境不要调用它（那等于删用户聊天记录和记忆）。
        """
        sid = session_id_for(user_id)
        had_agent = self._sessions.pop(sid, None) is not None
        self._locks.pop(sid, None)

        deleted = 0
        memory_deleted = 0
        if wipe_messages:
            try:
                from database import clear_session_chat

                deleted = clear_session_chat(sid, user_id)
            except Exception as exc:  # noqa: BLE001 - 清理失败不该中断评测
                log.warning("reset session db failed: %s: %s", sid, exc)

        if wipe_memory:
            try:
                from database import SessionLocal
                from models import MemoryOutbox, UserMemory
                from sqlalchemy import delete

                with SessionLocal() as s:
                    memory_deleted = s.execute(
                        delete(UserMemory).where(UserMemory.user_id == user_id)
                    ).rowcount or 0
                    s.execute(
                        delete(MemoryOutbox).where(MemoryOutbox.session_id == sid)
                    )
                    s.commit()
            except Exception as exc:  # noqa: BLE001
                log.warning("reset memory failed: %s: %s", sid, exc)

        log.info(
            "session reset: %s (agent=%s, msgs=%d, memories=%d)",
            sid, had_agent, deleted, memory_deleted,
        )
        return {
            "session_id": sid,
            "removed_agent": had_agent,
            "deleted_messages": deleted,
            "deleted_memories": memory_deleted,
        }


#: 进程内单例（单 worker 足够；多 worker 时要换 Redis）
_manager: SessionManager | None = None


def get_session_manager() -> SessionManager:
    """全局单例，默认接 MySQL 的聊天表（loader + persister）。"""
    global _manager
    if _manager is None:
        _manager = SessionManager(
            loader=_default_loader,
            persister=_default_persister,
            on_close=_default_on_close,
            tracer=_default_tracer,
        )
    return _manager
