"""记忆管理器的对外门面（Agent 持有它）。

Agent 只用两个动作：
- ``build_injection()``：每轮开始前拿注入块（记忆 + 数据新鲜度）；
- ``close_session()``：会话结束时投记忆提炼任务。

提炼/治理本身在 ``extract.py`` / ``manage.py``，由 outbox 离线消费。
"""
from __future__ import annotations

import logging

from .inject import build_injection as _build_injection

log = logging.getLogger(__name__)


class MemoryManager:
    def __init__(self, user_id: int) -> None:
        self.user_id = user_id

    async def build_injection(self) -> list[dict]:
        """把长期记忆 + 数据新鲜度拼成注入块（失败返回空）。"""
        return await _build_injection(self.user_id)

