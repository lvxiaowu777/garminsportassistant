"""Agent 池：**每用户一个 Agent，空闲回收**。

为什么需要池：Agent 持有 CM（对话历史），历史要在 1 小时内的多次对话之间存活，
所以 Agent 必须比"一次请求"活得久；但也不能永远不回收，否则内存只涨不降。

生命周期：
- 用户发消息 → ``get(user_id)``：池里有就直接用，没有就新建；
- 新建时：``sessions.open(user_id)`` 拿到今天的 Session，Agent 首次 run 时 resume；
- 空闲超过 ``SESSION_TTL_SECONDS``（默认 1h）或数量超限 → ``close()``：
  Session 关闭（投记忆提炼任务）并从 SessionManager 移除，Agent 丢弃；
- 下次发消息重建，历史从库里 resume。

跨天：``session_id`` 里的日期变了 → 新 Session；旧 Agent 空闲后自然淘汰。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time

from garmin_coach.agent.llmclient.config import get_provider
from garmin_coach.agent.llmclient.factory import get_shared_client
from garmin_coach.agent.conversation import ConversationManager
from garmin_coach.agent.loop import Agent
from garmin_coach.agent.memory import MemoryManager
from garmin_coach.agent.prompts import PromptProvider
from garmin_coach.agent.session import SessionManager, get_session_manager, session_id_for
from garmin_coach.agent.tools.health import build_default_registry
from garmin_coach.settings import settings

log = logging.getLogger(__name__)


def _current_loop_id() -> int:
    """当前事件循环 id；没有 loop（同步测试）时返回 0。"""
    try:
        return id(asyncio.get_running_loop())
    except RuntimeError:
        return 0


def _default_tracer(**kwargs) -> None:
    """把一次 run 的轨迹写进 agent_runs / agent_steps。"""
    from garmin_coach.services.trace import save_run

    save_run(**kwargs)


class AgentPool:
    """Agent 的缓存与回收。"""

    def __init__(
        self,
        sessions: SessionManager | None = None,
        *,
        ttl: int | None = None,
        max_agents: int | None = None,
        tracer=None,
    ) -> None:
        self._sessions = sessions or get_session_manager()
        self._agents: dict[int, Agent] = {}
        # 回收由 APScheduler 的后台线程触发，而 get() 在事件循环线程里跑；
        # 两个线程都会改 _agents，用一把进程内锁把遍历/删除串起来。
        # （真要多 worker 还是得换 Redis，见总纲 §2.12。）
        self._lock = threading.RLock()
        self._ttl = ttl if ttl is not None else settings.session_ttl_seconds
        self._max = max_agents if max_agents is not None else settings.max_sessions
        self._tracer = tracer or _default_tracer
        self._last_reap = time.time()

    # ── 取用 ──

    def get(self, user_id: int) -> Agent:
        """拿这个用户的 Agent；没有就建一个（会 resume 今天的会话）。

        三种重建：换事件循环、跨天、池里没有。整个读写都在锁内完成，
        因为 ``reap()`` 可能来自调度线程。
        """
        self._maybe_reap()
        current_loop = _current_loop_id()
        today_sid = session_id_for(user_id)

        with self._lock:
            agent = self._agents.get(user_id)

            if agent is not None and agent.loop_id != current_loop:
                # 换了事件循环（测试里每个请求一个 loop）：旧 Agent 的锁/客户端
                # 绑在已关闭的 loop 上，必须重建。历史在库里，重建后 resume。
                log.info("agent rebuilt for new event loop: user=%s", user_id)
                self._evict(user_id)
                agent = None

            if agent is not None and agent.session_id != today_sid:
                # 跨天但没空闲到被回收（比如 23:59 和 00:01 连着聊）：
                # 必须换到今天的会话，否则消息会记到昨天那个 session_id 上。
                log.info(
                    "agent rebuilt for new day: user=%s %s -> %s",
                    user_id, agent.session_id, today_sid,
                )
                self._evict(user_id)
                agent = None

            if agent is None:
                agent = self._build(user_id)
                self._agents[user_id] = agent
                log.info(
                    "agent created: user=%s session=%s provider=%s",
                    user_id, agent.session_id, agent.provider,
                )
            agent.touch()
            return agent

    def _build(self, user_id: int) -> Agent:
        provider = get_provider()
        if not provider.api_key:
            raise RuntimeError(f"{provider.display_name} 的 API key 未配置")

        session = self._sessions.open(user_id)
        conv = ConversationManager(user_id=user_id, session=session)
        agent = Agent(
            llm=get_shared_client(provider),          # 传输层：所有 Agent 共享
            registry=build_default_registry(user_id),  # 工具层：绑定 user_id
            prompts=PromptProvider(),                  # 提示词：依赖注入
            conversation=conv,                         # 消息本 + 持久化（持有 Session）
            memory=MemoryManager(user_id),             # 记忆：注入 + 提炼
            user_id=user_id,
            provider=provider.key,
            tracer=self._tracer,
            max_turns=settings.agent_max_turns,
            temperature=settings.agent_temperature,
            max_tokens=provider.max_tokens,
        )
        agent.loop_id = _current_loop_id()
        return agent

    # ── 回收 ──

    def _maybe_reap(self) -> None:
        if time.time() - self._last_reap >= 60:
            self.reap()

    def reap(self) -> int:
        """回收空闲超时 / LRU 超限的 Agent；**跳过正在跑的**。"""
        self._last_reap = time.time()
        with self._lock:
            before = len(self._agents)

            expired = [
                uid for uid, a in self._agents.items()
                if a.idle_seconds > self._ttl and not a.is_busy
            ]
            for uid in expired:
                self._evict(uid)

            if len(self._agents) > self._max:
                idle = [
                    (uid, a) for uid, a in self._agents.items() if not a.is_busy
                ]
                idle.sort(key=lambda kv: kv[1].last_active)
                for uid, _ in idle[: len(self._agents) - self._max]:
                    self._evict(uid)

            removed = before - len(self._agents)
        if removed:
            log.info("agent reap: removed=%d alive=%d", removed, len(self._agents))
        return removed

    def _evict(self, user_id: int) -> None:
        with self._lock:
            agent = self._agents.pop(user_id, None)
        if agent is None:
            return
        try:
            agent.close()                 # 关 Session：投记忆提炼任务
            self._sessions.discard(agent.session_id)
        except Exception as exc:  # noqa: BLE001 - 回收不能中断
            log.warning("agent evict failed: user=%s: %s", user_id, exc)

    # ── 评测 / 测试 ──

    def drop(self, user_id: int) -> None:
        """只清内存（库里的记录保留，下次会 resume）。"""
        agent = self._agents.pop(user_id, None)
        if agent is not None:
            agent.close()
        self._sessions.drop(user_id)

    def reset(self, user_id: int, *, wipe_memory: bool = False) -> dict:
        """评测专用：内存 + 库 + 记忆一起清。"""
        agent = self._agents.pop(user_id, None)
        if agent is not None:
            agent.close()
        return self._sessions.reset(user_id, wipe_messages=True, wipe_memory=wipe_memory)

#: 进程内单例（单 worker；多 worker 要换 Redis）
_pool: AgentPool | None = None


def get_agent_pool() -> AgentPool:
    global _pool
    if _pool is None:
        _pool = AgentPool()
    return _pool


def get_agent(user_id: int) -> Agent:
    """路由层唯一入口：给我这个用户的 Agent。"""
    return get_agent_pool().get(user_id)
