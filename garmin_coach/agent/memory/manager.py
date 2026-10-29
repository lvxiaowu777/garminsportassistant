"""记忆管理器的对外门面（Agent 持有它）。

Agent 只用两个动作：
- ``memory_blocks()``：每个 Agent 生命周期注入一次（长期记忆）；
- ``freshness_blocks()``：每轮注入（数据新鲜度会变）。

提炼/治理本身在 ``extract.py`` / ``manage.py``，由 outbox 离线消费。
"""
from __future__ import annotations

import logging

from .inject import build_injected as _build_memory

log = logging.getLogger(__name__)


class MemoryManager:
    def __init__(self, user_id: int) -> None:
        self.user_id = user_id

    def memory_blocks(self) -> list[dict]:
        """长期记忆块（同步读库，失败返回空）。每个 Agent 只调一次。"""
        try:
            return _build_memory(self.user_id) or []
        except Exception as exc:  # noqa: BLE001
            log.warning("memory injection failed: user=%s: %s", self.user_id, exc)
            return []

    async def freshness_blocks(self) -> list[dict]:
        """数据新鲜度块（每轮都算：数据可能刚刚更新过）。"""
        try:
            from garmin_coach.services.freshness import data_note, ensure_fresh

            fresh = await ensure_fresh(self.user_id)
            return [{"role": "system", "content": data_note(fresh)}]
        except Exception as exc:  # noqa: BLE001
            log.warning("freshness injection failed: user=%s: %s", self.user_id, exc)
            return []

