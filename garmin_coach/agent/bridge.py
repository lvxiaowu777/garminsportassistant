"""HTTP 层与 Agent 之间的桥。

``src/server.py`` 只调这里的一个函数，不关心 Agent 内部怎么转。
这样将来换传输协议、加流式，都不用改路由。

设计上保留了「工具调用结果」的完整留痕（``AgentResult.tool_calls``），
路由层用它判断「这一轮有没有写训练记录」，比旧版靠正则解析 ``[RECORD]``
标记可靠得多。
"""
from __future__ import annotations

import logging

from garmin_coach.llm.config import get_provider
from garmin_coach.llm.factory import create_client
from garmin_coach.tools.health import build_default_registry

from .loop import Agent, AgentResult

log = logging.getLogger(__name__)


async def run_agent_chat(
    message: str,
    user_id: int = 0,
    *,
    history: list[dict] | None = None,
    provider_name: str | None = None,
    max_turns: int = 8,
) -> AgentResult:
    """跑一轮带工具的对话，返回结构化结果。

    抛 ``RuntimeError`` 表示「没有可用的 API key」，调用方应当据此决定
    是回退旧实现还是直接把错误告诉用户。
    """
    provider = get_provider(provider_name)
    if not provider.api_key:
        raise RuntimeError(f"{provider.display_name} 的 API key 未配置")

    llm = create_client(provider)
    # 工具绑定 user_id：工具层没有 Depends 可用，身份必须在这里注入
    registry = build_default_registry(user_id)

    agent = Agent(
        llm,
        registry,
        max_turns=max_turns,
        max_tokens=provider.max_tokens,
    )
    return await agent.run(message, history=history)
