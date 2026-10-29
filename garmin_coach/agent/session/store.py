"""会话的**数据通道**：只做三件事 —— 恢复、落库、结束。

一个 Session 不对应任何运行时状态（没有锁、没有 Agent、没有 CM）：
- ``resume()``：从库里读这个会话的历史消息，交给 Agent 灌进 CM；
- ``append(role, content)``：写一条消息；
- ``close()``：投一条记忆提炼待办（离线消费）。

会话的**身份**由 ``session_id`` 决定：``u{user_id}-{YYYY-MM-DD}``，一天一个。
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: 恢复时最多带多少条（≈100 轮对话）。一天内正常聊不到这个量；
#: 上限只是防止极端会话撑爆上下文，等做了上下文压缩可以再放大。
RESUME_LIMIT = 200


def session_id_for(user_id: int, day: dt.date | None = None) -> str:
    """会话 id 规则：一天一个 —— ``u1001-2026-10-26``。

    ``SESSION_ID_OVERRIDE`` 供评测使用：给每次评测一个独立会话，
    这样跑评测不会清掉用户真实的当天聊天。
    """
    import os

    override = os.environ.get("SESSION_ID_OVERRIDE", "").strip()
    if override:
        return override
    return f"u{user_id}-{(day or dt.date.today()).isoformat()}"


@dataclass
class Session:
    """一次会话的数据句柄。"""

    session_id: str
    user_id: int
    closed: bool = False

    # ── 恢复 ──

    def resume(self, limit: int = RESUME_LIMIT) -> list[dict]:
        """读历史消息（失败返回空：没有历史也要能聊）。"""
        try:
            from garmin_coach.db.database import get_chat_history, get_last_compact_id

            # 有压缩边界就从边界那条开始读（边界之前的内容已经在摘要里）
            boundary = get_last_compact_id(self.user_id, self.session_id)
            after_id = boundary - 1 if boundary is not None else None
            rows = get_chat_history(
                limit,
                self.user_id,
                session_id=self.session_id,
                after_id=after_id,
            ) or []
            # 中断占位行只给 UI 看，不进模型上下文（否则它会把报错当成自己说过的话）
            return [r for r in rows if r.get("type") != "error"]
        except Exception as exc:  # noqa: BLE001 - 恢复失败不该让对话失败
            log.warning("session resume failed: %s: %s", self.session_id, exc)
            return []

    # ── 落库 ──

    def append(self, role: str, content: str, **extra) -> None:
        """写一条消息。失败只记日志（已经发生的事实不因落库失败而改变）。

        ``extra`` 透传给 ``add_chat``（语音消息用：msg_type / audio_url / duration_sec）。
        """
        if not content:
            return
        try:
            from garmin_coach.db.database import add_chat

            add_chat(role, content, self.user_id, session_id=self.session_id, **extra)
        except Exception as exc:  # noqa: BLE001
            log.warning("session append failed (%s): %s: %s", role, self.session_id, exc)

    # ── 结束 ──

    def close(self) -> None:
        """会话结束：只标记关闭，**不触发记忆提炼**。

        记忆提炼统一放到每天 04:07 的夜间任务（扫前一天及漏掉的会话），
        避免"聊一小时就总结一次"这种过度提炼，也少一次写库。
        """
        self.closed = True

