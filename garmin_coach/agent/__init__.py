"""Agent 主循环 —— 一个会话一个实例，run() 是事件生成器。

模块划分：

``loop.py``           ``Agent.run()``：模型 ↔ 工具的循环，逐事件产出
``events.py``         Agent 层事件（TextEvent / ToolCallEvent / DoneEvent…）
``conversation.py``   ``ConversationManager``：消息历史、协议合法性与序列化
``session.py``        ``SessionManager``：会话生命周期、落库、恢复、回收
``prompts.py``        系统提示词（含时间注入与全部领域规则）
``bridge.py``         HTTP 层调用的入口（装配会话，供路由调用）

设计要点：这一层**不碰 HTTP**。数据通过工具拿，结果通过事件给，
所以它可以直接被单元测试驱动（用假 LLM 和假工具）。
"""
from .bridge import get_session_manager, run_agent_chat
from .conversation import ConversationManager
from .events import (
    AgentEvent,
    AgentResult,
    DoneEvent,
    ErrorEvent,
    TextEvent,
    ThinkingEvent,
    ToolCallEvent,
    ToolCallRecord,
    ToolResultEvent,
)
from .loop import Agent
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
    "TextEvent",
    "ThinkingEvent",
    "ToolCallEvent",
    "ToolCallRecord",
    "ToolResultEvent",
    "build_system_prompt",
    "get_session_manager",
    "run_agent_chat",
    "session_id_for",
]
