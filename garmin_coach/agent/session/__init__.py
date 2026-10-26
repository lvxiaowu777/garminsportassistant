"""会话：Session 是数据通道（恢复/落库/结束），SessionManager 管生命周期。"""
from .manager import SessionManager, get_session_manager
from .store import Session, session_id_for

__all__ = ["Session", "SessionManager", "get_session_manager", "session_id_for"]
