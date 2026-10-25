"""对话历史管理 —— 对应 MewCode 的 ``ConversationManager``。

只做三件事：存消息、按需加载历史、保证消息序列对 API 合法。

「保证合法」这一条比看起来重要：OpenAI 协议要求带 tool_calls 的 assistant
消息后面必须紧跟对应的 tool 消息，少一条就 400。所以这里不提供「随便 append
一条 dict」的入口，只能通过语义化的方法写入。
"""
from __future__ import annotations

import json
from typing import Any

from .serialize import serialize_messages


class ConversationManager:
    """一次会话的消息本。

    ``user_id`` / ``session_id`` 只是**标识**，用来在日志和 trace 里对上号；
    它不知道数据库、不知道 TTL —— 落库和回收是 SessionManager 的事。
    """

    def __init__(self, user_id: int = 0, session_id: str = "") -> None:
        self.user_id = user_id
        self.session_id = session_id
        self.messages: list[dict[str, Any]] = []

    # ── 加载历史 ──

    def load_history(self, history: list[dict] | None, limit: int = 20) -> None:
        """把历史消息并进来（数据库只存 user/assistant 纯文本）。

        做三件清洗，理由是「发出去的消息必须合法」：

        1. 只收 user / assistant，丢掉未知角色（tool 消息属于某一轮，不跨请求）；
        2. **相邻同角色合并** —— 落库是按条写的，崩溃可能留下
           `user, user` 这种半条记录；合并成一条更稳，也满足 Anthropic
           要求的 user/assistant 交替；
        3. 丢掉空内容。
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
            if self.messages and self.messages[-1]["role"] == role:
                self.messages[-1]["content"] = (
                    f"{self.messages[-1]['content']}\n{content}"
                )
            else:
                self.messages.append({"role": role, "content": content})

    # ── 序列化（发给 LLM 前必须过这一关）──

    def serialize(self) -> list[dict[str, Any]]:
        """规整成可直接 JSON 化的协议合法形态（实现在 serialize.py）。"""
        return serialize_messages(self.messages)

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_assistant(self, text: str) -> None:
        self.messages.append({"role": "assistant", "content": text})

    def add_assistant_tool_calls(
        self,
        text: str,
        calls: list[tuple[str, str, dict[str, Any]]],
    ) -> None:
        """记录模型请求调用工具的那一轮。

        ``calls`` 每项是 ``(call_id, tool_name, arguments)``。
        arguments 要序列化回字符串 —— 这是协议要求，不是我们的选择。
        """
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
        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": content,
            }
        )

    # ── 辅助 ──

    def clear(self) -> None:
        self.messages.clear()

    def estimate_chars(self) -> int:
        """粗略的上下文体积（字符数）。

        这里不做精确 token 计数 —— 中文下 1 token ≈ 1.5 字符，
        真要精确得引 tokenizer。用于观察和日志足够。
        """
        total = 0
        for m in self.messages:
            total += len(str(m.get("content") or ""))
            for tc in m.get("tool_calls") or []:
                total += len(tc.get("function", {}).get("arguments") or "")
        return total

    def __len__(self) -> int:
        return len(self.messages)
