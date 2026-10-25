"""session/session.py：Session 数据类 + id 规则（从 manager 拆出来）。"""
from __future__ import annotations

import asyncio
import datetime as dt
import time
from dataclasses import dataclass, field

from ..loop import Agent


def session_id_for(user_id: int, day: dt.date | None = None) -> str:
    """会话 id 规则：一天一个 —— ``u1001-2026-10-25``。"""
    return f"u{user_id}-{(day or dt.date.today()).isoformat()}"


def _current_loop_id() -> int:
    """当前事件循环的 id；没有 loop 时返回 0。"""
    try:
        return id(asyncio.get_running_loop())
    except RuntimeError:
        return 0


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
    #: 创建时所在的事件循环。跨 loop 复用同一个 Agent 会拿到已关闭的 loop
    #: （测试里每个请求一个 loop，uvicorn 生产环境只有一个）。
    loop_id: int = 0

    def touch(self) -> None:
        self.last_active = time.time()

    @property
    def idle_seconds(self) -> float:
        return time.time() - self.last_active
