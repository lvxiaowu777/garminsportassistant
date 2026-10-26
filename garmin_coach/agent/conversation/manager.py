"""对话管理器：**内存消息本 + 持久化门面**。

Agent 只跟 CM 打交道，不直接碰数据库：

- 内存：``messages``（协议合法化交给 ``serialize.py``）
- 恢复：``load()`` 显式调一次 ``Session.resume()``（不在构造里做 IO）
- 写入分两类：
  - ``add_user`` / ``add_assistant``：进内存 **并落库**（用户和最终回答）；
  - ``add_assistant_tool_calls`` / ``add_tool_result``：只进内存（工具往返不落库）；
  - ``fail``：**只落库**，不进内存（错误占位不该被模型当成自己的话）。

这样"什么时候进 messages、什么时候落库"只有一个入口，Agent 的 loop 不用再管持久化。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from ..session.store import Session
from .serialize import serialize_messages

log = logging.getLogger(__name__)


class ConversationManager:
    """一次会话的消息本（同时是持久化的唯一入口）。"""

    def __init__(self, user_id: int = 0, session: Session | None = None) -> None:
        self.user_id = user_id
        self._session = session
        self.messages: list[dict[str, Any]] = []
        self._loaded = False

    # ── 身份 ──

    @property
    def session_id(self) -> str:
        return self._session.session_id if self._session else ""

    def attach(self, session: Session) -> None:
        """换绑会话（评测/重建用）：历史重置为未加载。"""
        self._session = session
        self._loaded = False

    # ── 恢复 ──

    def load(self) -> None:
        """首次使用时从库里恢复历史；之后不再读库（内存为准）。"""
        if self._loaded:
            return
        self._loaded = True
        if self._session is not None:
            self.load_history(self._session.resume())

    def load_history(self, history: list[dict] | None, limit: int = 20) -> None:
        """把历史消息并进来（数据库只存 user/assistant 纯文本）。

        这里只做**过滤**：只收 user / assistant（tool 属于某一轮，不跨请求），
        丢掉空内容。**相邻同角色合并留给 ``serialize()``**——那是发送前唯一的
        规整入口，恢复阶段不做二次加工，内存里的消息与库里一一对应。
        """
        if not history:
            return
        for item in history[-limit:]:
            role = item.get("role")
            if role not in ("user", "assistant"):
                continue
            content = (item.get("content") or "").strip()
            if not content:
                continue
            self.messages.append({"role": role, "content": content})

    # ── 写：进内存 + 落库 ──

    def add_user(self, text: str, **meta: Any) -> None:
        """用户消息：进内存并落库（``meta`` 给语音：msg_type/audio_url/duration_sec）。"""
        self.messages.append({"role": "user", "content": text})
        self._persist("user", text, **meta)

    def add_assistant(self, text: str) -> None:
        """**最终回答**：进内存并落库（这才是聊天记录里该有的那条）。"""
        self.messages.append({"role": "assistant", "content": text})
        self._persist("assistant", text)

    # ── 写：只进内存（工具往返）──

    def add_assistant_tool_calls(
        self,
        text: str,
        calls: list[tuple[str, str, dict[str, Any]]],
    ) -> None:
        """模型请求调用工具的那一轮（不落库：用户不需要看到这段）。"""
        self.messages.append(
            {
                "role": "assistant",
                "content": text or None,
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(args, ensure_ascii=False),
                        },
                    }
                    for call_id, name, args in calls
                ],
            }
        )

    def add_tool_result(self, call_id: str, content: str) -> None:
        """工具结果（不落库：属于这一轮的临时数据）。"""
        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": content,
            }
        )

    # ── 写：只落库（错误占位）──

    def fail(self, text: str) -> None:
        """落一条 ``type=error`` 的占位，**不进内存**。

        恢复时 ``Session.resume()`` 会过滤掉它，所以模型不会把报错当历史发言；
        但用户刷新页面能看到"这条没生成完"。
        """
        self._persist("assistant", text, msg_type="error")

    # ── 持久化 ──

    def _persist(self, role: str, content: str, **meta: Any) -> None:
        if self._session is None or not content:
            return
        try:
            self._session.append(role, content, **meta)
        except Exception as exc:  # noqa: BLE001 - 落库失败不该影响回答
            log.warning("conversation persist failed: %s: %s", self.session_id, exc)

    # ── 辅助 ──

    def serialize(self) -> list[dict[str, Any]]:
        """规整成可直接 JSON 化的协议合法形态（实现在 serialize.py）。"""
        return serialize_messages(self.messages)

    def clear(self) -> None:
        """清内存（DB 由 SessionManager.reset 负责）。"""
        self.messages.clear()
        self._loaded = True

    def close(self) -> None:
        """会话结束：投记忆提炼任务。"""
        if self._session is not None:
            self._session.close()

    def estimate_chars(self) -> int:
        """粗略的上下文体积（字符数）。"""
        total = 0
        for m in self.messages:
            total += len(str(m.get("content") or ""))
            for tc in m.get("tool_calls") or []:
                total += len(tc.get("function", {}).get("arguments") or "")
        return total

    def __len__(self) -> int:
        return len(self.messages)
