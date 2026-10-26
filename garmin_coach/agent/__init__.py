"""Agent 层 —— **Agent 是组合根**，持有 CM / SessionManager / 工具 / 提示词 / 记忆 / trace。

```
loop/          Agent（组合根 + 核心 loop）+ Agent 层事件
session/       Session（数据通道：resume/append/close）+ SessionManager（生命周期）
conversation/  消息本 + 协议合法化（serialize）
prompts/       提示词提供者（PromptProvider）
tools/         工具基类 / 注册表 / 具体工具
llmclient/     传输层：OpenAI 兼容 / Anthropic 原生 / 配置 / 工厂
memory/        长期记忆：存储 / 注入 / 提炼 / 管理 / outbox
pool.py        AgentPool：每用户一个 Agent，空闲 1h 回收
```

路由只认 ``get_agent(user_id)``；Agent 的 ``run()`` 是唯一入口，
落库、trace、会话恢复都在它内部完成。
"""
from .conversation import ConversationManager, serialize_messages
from .loop import (
    Agent,
    AgentEvent,
    AgentResult,
    DoneEvent,
    ErrorEvent,
    Step,
    TextEvent,
    ThinkingEvent,
    ToolCallEvent,
    ToolCallRecord,
    ToolResultEvent,
    to_sse,
)
from .memory import MemoryManager
from .pool import AgentPool, get_agent, get_agent_pool
from .prompts import PromptProvider, build_system_prompt
from .session import Session, SessionManager, get_session_manager, session_id_for

__all__ = [
    "Agent",
    "AgentEvent",
    "AgentPool",
    "AgentResult",
    "ConversationManager",
    "DoneEvent",
    "ErrorEvent",
    "MemoryManager",
    "PromptProvider",
    "Session",
    "SessionManager",
    "Step",
    "TextEvent",
    "ThinkingEvent",
    "ToolCallEvent",
    "ToolCallRecord",
    "ToolResultEvent",
    "build_system_prompt",
    "get_agent",
    "get_agent_pool",
    "get_session_manager",
    "serialize_messages",
    "session_id_for",
    "to_sse",
]
