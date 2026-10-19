"""Agent 主循环 —— 对应 MewCode 的 ``Agent.run()``。

核心就是一个循环：问模型 → 模型要么直接答，要么要求调工具 →
执行工具把结果塞回对话 → 再问一次。直到模型不再要求工具。

这就是「Agent」和「单轮 RAG」的全部区别。旧实现把数据一次性拼进 prompt，
模型只有一次回答机会；这里模型可以自己决定查什么、查几次。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from garmin_coach.llm.base import (
    LLMClient,
    MessageStop,
    StreamError,
    TextDelta,
    ThinkingDelta,
    ToolUseEnd,
)
from garmin_coach.tools.registry import ToolRegistry

from .conversation import ConversationManager
from .prompts import build_system_prompt

log = logging.getLogger(__name__)


@dataclass
class ToolCallRecord:
    """一次工具调用留痕，用于日志和调试（不出现在用户回复里）。"""

    name: str
    arguments: dict[str, Any]
    result: str
    is_error: bool = False


@dataclass
class AgentResult:
    text: str
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    turns: int = 1
    usage: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    truncated: bool = False

    @property
    def tools_used(self) -> list[str]:
        return [c.name for c in self.tool_calls]


class Agent:
    """带工具调用能力的对话 Agent。

    参数说明：
    - ``max_turns``：最多几轮「模型 ↔ 工具」往返。设上限是为了防止模型
      在两个工具之间无限来回（真实发生过）。到上限就带着已有文本收尾。
    - ``registry``：每个用户一套（工具绑定了 user_id），不复用。
    """

    def __init__(
        self,
        llm: LLMClient,
        registry: ToolRegistry,
        *,
        max_turns: int = 8,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> None:
        self._llm = llm
        self._registry = registry
        self._max_turns = max_turns
        self._temperature = temperature
        self._max_tokens = max_tokens

    async def run(
        self,
        user_message: str,
        history: list[dict] | None = None,
        *,
        system_prompt: str | None = None,
        on_text: Any = None,
    ) -> AgentResult:
        """跑一轮完整对话，返回最终回复。

        ``on_text`` 是可选回调，每收到一段正文增量就调一次，
        用于将来接 WebSocket 打字机效果；不传就纯粹攒到结束。
        """
        conv = ConversationManager()
        conv.load_history(history)
        conv.add_user(user_message)

        system = system_prompt or build_system_prompt()
        schemas = self._registry.schemas()
        records: list[ToolCallRecord] = []
        usage: dict[str, int] = {}
        last_text = ""

        for turn in range(1, self._max_turns + 1):
            text_parts: list[str] = []
            pending_calls: list[tuple[str, str, dict[str, Any]]] = []
            stream_error: str | None = None
            stop_reason = ""

            async for event in self._llm.stream(
                conv.messages,
                system=system,
                tools=schemas or None,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
            ):
                if isinstance(event, TextDelta):
                    text_parts.append(event.text)
                    if on_text is not None:
                        on_text(event.text)
                elif isinstance(event, ToolUseEnd):
                    pending_calls.append((event.id, event.name, event.arguments))
                elif isinstance(event, MessageStop):
                    stop_reason = event.stop_reason
                    if event.usage:
                        usage = event.usage
                elif isinstance(event, ThinkingDelta):
                    # 思考内容不进对话，只记日志（对调试模型决策有用）
                    log.debug("thinking: %s", event.text[:200])
                elif isinstance(event, StreamError):
                    stream_error = event.message

            text = "".join(text_parts).strip()
            if text:
                last_text = text

            if stream_error:
                log.warning("LLM 流错误 turn=%d: %s", turn, stream_error)
                return AgentResult(
                    text=last_text or f"模型调用失败：{stream_error}",
                    tool_calls=records,
                    turns=turn,
                    usage=usage,
                    error=stream_error,
                )

            # 模型没要求工具 → 本轮就是最终答复
            if not pending_calls:
                conv.add_assistant(text)
                return AgentResult(
                    text=text,
                    tool_calls=records,
                    turns=turn,
                    usage=usage,
                )

            # 模型要求调工具：先把这一轮的 assistant 消息落进对话，
            # 否则下一步的 tool 消息找不到对应的 tool_calls（协议会 400）
            conv.add_assistant_tool_calls(text, pending_calls)

            for call_id, name, arguments in pending_calls:
                log.info("tool call: %s(%s)", name, arguments)
                result = await self._registry.execute(name, arguments)
                conv.add_tool_result(call_id, result.content)
                records.append(
                    ToolCallRecord(
                        name=name,
                        arguments=arguments,
                        result=result.content,
                        is_error=result.is_error,
                    )
                )

            log.info(
                "turn %d done, tools=%s, ctx_chars=%d",
                turn,
                [r.name for r in records],
                conv.estimate_chars(),
            )

        # 到轮次上限：模型还在要工具，但我们必须收尾
        log.warning("达到最大轮次 %d，强制结束", self._max_turns)
        return AgentResult(
            text=last_text or "（这轮查询太久了，先说说目前看到的情况，需要更细的再说）",
            tool_calls=records,
            turns=self._max_turns,
            usage=usage,
            truncated=True,
        )
