"""Agent 层 —— 一个会话一个实例，`run()` 是事件生成器。

子包划分（对应文件组织）：

```
loop/          Agent 循环 + Agent 层事件
session/       Session 数据 + SessionManager（生命周期 / 落库 / 恢复 / 回收）
conversation/  消息本 + 协议合法化（serialize）
prompts/       系统提示词（唯一一份）
tools/         工具基类 / 注册表 / 具体工具
llmclient/     传输层：OpenAI 兼容 / Anthropic 原生 / 配置 / 工厂
memory/        长期记忆：存储 / 注入 / 提炼 / 管理 / outbox
bridge.py      HTTP 层调用的入口（装配会话，供路由调用）
```

设计要点：这一层**不碰 HTTP**。数据通过工具拿，结果通过事件给，
所以它可以直接被单元测试驱动（用假 LLM 和假工具）。
"""
from .bridge import get_session_manager, run_agent_chat, stream_agent_chat
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
from .prompts import build_system_prompt
from .session import Session, SessionManager, session_id_for

__all__ = [
    "Agent",
    "AgentEvent",
    "AgentResult",
    "ConversationManager",
    "DoneEvent",
    "ErrorEvent",
    "Session",
    "SessionManager",
    "Step",
    "TextEvent",
    "ThinkingEvent",
    "ToolCallEvent",
    "ToolCallRecord",
    "ToolResultEvent",
    "build_system_prompt",
    "get_session_manager",
    "run_agent_chat",
    "serialize_messages",
    "session_id_for",
    "stream_agent_chat",
    "to_sse",
]
