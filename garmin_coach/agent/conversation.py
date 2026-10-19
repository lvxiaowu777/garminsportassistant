"""对话历史管理 —— 对应 MewCode 的 ``ConversationManager``。

只做三件事：存消息、按需加载历史、保证消息序列对 API 合法。

「保证合法」这一条比看起来重要：OpenAI 协议要求带 tool_calls 的 assistant
消息后面必须紧跟对应的 tool 消息，少一条就 400。所以这里不提供「随便 append
一条 dict」的入口，只能通过语义化的方法写入。
"""
from __future__ import annotations

import json
from typing import Any


class ConversationManager:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    # ── 加载历史 ──

    def load_history(self, history: list[dict] | None, limit: int = 20) -> None:
        """把数据库里的历史聊天记录并进来。

        只接受 user / assistant 两种纯文本消息 —— 历史里不该有 tool 消息，
        那些只属于「本轮」。加载时丢掉空内容和未知角色。
        """
        if not history:
            return
        for item in history[-limit:]:
            role = item.get("role")
            if role not in ("user", "assistant"):
                continue
            content = (item.get("content") or "").strip()
            if content:
                self.messages.append({"role": role, "content": content})

    # ── 写入 ──

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
