"""HTTP 层与 Agent 之间的桥。

路由只调这里的函数，不关心 Agent 内部怎么转。
会话的取用/回收/落库交给 ``SessionManager``，这里只做「校验 provider + 注入 + 转发」。
"""
from __future__ import annotations

import logging
from typing import AsyncIterator

from garmin_coach.llm.config import get_provider
from garmin_coach.settings import settings

from .events import AgentEvent
from .loop import AgentResult
from .session import SessionManager, get_session_manager

log = logging.getLogger(__name__)

__all__ = [
    "run_agent_chat",
    "stream_agent_chat",
    "get_session_manager",
    "SessionManager",
]


def _check_provider(provider_name: str | None) -> None:
    provider = get_provider(provider_name)
    if not provider.api_key:
        raise RuntimeError(f"{provider.display_name} 的 API key 未配置")


def _injected_for(user_id: int) -> list[dict] | None:
    """长期记忆注入（读失败就退化成空，不能影响对话）。"""
    try:
        from garmin_coach.services.memory import build_injected

        return build_injected(user_id)
    except Exception:  # noqa: BLE001
        return None


async def run_agent_chat(
    message: str,
    user_id: int = 0,
    *,
    history: list[dict] | None = None,
    provider_name: str | None = None,
    max_turns: int | None = None,
    persist_user: bool = True,
) -> AgentResult:
    """跑一轮带工具的对话，返回结构化结果。

    - ``history``：只有会话**首次**使用时会被用来 hydrate（之后走内存）
    - ``persist_user=False``：调用方自己写过 user 消息（语音消息带 audio_url）
    - 抛 ``RuntimeError`` 表示没有可用 API key
    """
    _check_provider(provider_name)
    return await get_session_manager().run_turn(
        user_id,
        message,
        hydrate=history,
        injected=_injected_for(user_id),
        provider_name=provider_name,
        max_turns=max_turns if max_turns is not None else settings.agent_max_turns,
        persist_user=persist_user,
    )


async def stream_agent_chat(
    message: str,
    user_id: int = 0,
    *,
    history: list[dict] | None = None,
    provider_name: str | None = None,
    max_turns: int | None = None,
    persist_user: bool = True,
) -> AsyncIterator[AgentEvent]:
    """跑一轮并逐事件产出（SSE / WS 用）。

    落库与 trace 由 ``SessionManager.stream_turn`` 统一负责，
    所以流式路径和非流式路径的历史记录是一致。
    """
    _check_provider(provider_name)
    async for event in get_session_manager().stream_turn(
        user_id,
        message,
        hydrate=history,
        injected=_injected_for(user_id),
        provider_name=provider_name,
        max_turns=max_turns if max_turns is not None else settings.agent_max_turns,
        persist_user=persist_user,
    ):
        yield event
