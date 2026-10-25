"""对话历史：内存消息本 + 协议合法化。"""
from .manager import ConversationManager
from .serialize import serialize_messages

__all__ = ["ConversationManager", "serialize_messages"]
