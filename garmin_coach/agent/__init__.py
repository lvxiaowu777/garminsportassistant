"""Agent 主循环 —— 对应 MewCode 的 ``mewcode/agent.py``。

模块划分：

``loop.py``           ``Agent.run()``：模型 ↔ 工具的循环，直到模型不再要工具
``conversation.py``   ``ConversationManager``：消息历史与协议合法性
``prompts.py``        系统提示词（含时间注入与全部领域规则）
``bridge.py``         HTTP 层调用的入口，负责装配 LLM + 工具 + Agent

设计要点：这一层**不碰数据库、不碰 HTTP**。数据通过工具拿，结果通过返回值给，
所以它可以直接被单元测试驱动（用假 LLM 和假工具）。
"""
from .bridge import run_agent_chat
from .conversation import ConversationManager
from .loop import Agent, AgentResult, ToolCallRecord
from .prompts import build_system_prompt

__all__ = [
    "Agent",
    "AgentResult",
    "ConversationManager",
    "ToolCallRecord",
    "build_system_prompt",
    "run_agent_chat",
]
