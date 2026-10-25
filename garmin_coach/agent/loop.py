"""Agent 主循环 —— 一个**异步生成器**，边跑边吐事件。

核心还是那个循环：问模型 → 模型要么直接答，要么要求调工具 →
执行工具把结果塞回对话 → 再问一次。区别是现在每一步都 ``yield`` 出来：

```
async for ev in agent.run("今晚跑不跑"):
    if isinstance(ev, TextEvent): 前端追加文字
    elif isinstance(ev, ToolCallEvent): 显示"正在查天气…"
    elif isinstance(ev, DoneEvent): 收尾
```

想要「一次拿到最终结果」的调用方用 ``SessionManager.run_turn()``（它就是
把事件流消费完再返回 ``AgentResult``，落库/trace 也一并做完）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator

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
from .events import (
    AgentEvent,
    AgentResult,
    DoneEvent,
    ErrorEvent,
    Step,
    TextEvent,
    ThinkingEvent,
    ToolCallEvent,
    ToolCallRecord,
    ToolResultEvent,
)
from .prompts import build_system_prompt

log = logging.getLogger(__name__)

__all__ = [
    "Agent",
    "AgentResult",
    "AgentEvent",
    "TextEvent",
    "ThinkingEvent",
    "ToolCallEvent",
    "ToolResultEvent",
    "DoneEvent",
    "ErrorEvent",
    "Step",
    "ToolCallRecord",
]


class Agent:
    """带工具调用能力的对话 Agent。

    参数说明：
    - ``llm``：**共享**的传输层客户端（按 provider 缓存，不要每个 Agent 一个）
    - ``registry``：每个用户一套（工具绑定了 user_id），不能跨用户复用
    - ``user_id`` / ``session_id``：身份标识，CM 和日志用
    - ``max_turns``：最多几轮「模型 ↔ 工具」往返。设上限是为了防止模型
      在两个工具之间无限来回（真实发生过）。到上限就带着已有文本收尾。

    **生命周期**：一个实例对应一个会话，由 ``SessionManager`` 持有；
    CM 在构造时创建，跨轮次存活，不再每次 run 重建。
    """

    def __init__(
        self,
        llm: LLMClient,
        registry: ToolRegistry,
        *,
        user_id: int = 0,
        session_id: str = "",
        max_turns: int = 8,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> None:
        self._llm = llm
        self._registry = registry
        self.user_id = user_id
        self.session_id = session_id
        self._conv = ConversationManager(user_id=user_id, session_id=session_id)
        self._lock = asyncio.Lock()
        self._max_turns = max_turns
        self._temperature = temperature
        self._max_tokens = max_tokens
        # 注入块（快照摘要 / 记忆 / 提醒）：放在 system 之后、对话之前。
        # 由外部每轮换（Agent 不自己拼业务数据）。
        self._injected: list[dict[str, Any]] = []

    @property
    def conversation(self) -> ConversationManager:
        return self._conv

    def set_injected(self, injected: list[dict[str, Any]] | None) -> None:
        """设置本轮的注入块（如「今日档位 EASY / 数据截至 07:30」）。

        注入块不计入 CM，也不会落库 —— 它只影响这一次请求的 payload。
        """
        self._injected = list(injected or [])

    def _payload(self, system: str | None) -> list[dict[str, Any]]:
        """拼出最终发给模型的 messages 数组（**直接可 JSON 化**）。

        顺序：system → 注入块 → 会话消息。system 也在这里进数组，
        而不是交给传输层再加一层前缀。
        """
        payload: list[dict[str, Any]] = []
        if system:
            payload.append({"role": "system", "content": system})
        payload.extend(self._injected)
        payload.extend(self._conv.serialize())
        return payload

    # ── 对外：事件流 ──

    async def run(
        self,
        user_message: str,
        hydrate: list[dict] | None = None,
        *,
        injected: list[dict[str, Any]] | None = None,
        system_prompt: str | None = None,
        on_text: Any = None,
    ) -> AsyncIterator[AgentEvent]:
        """跑一轮，**逐事件产出**（TextEvent / ToolCallEvent / DoneEvent…）。

        - ``hydrate``：首次使用时从数据库补历史（只补一次，之后全靠内存）
        - ``injected``：本轮注入块（快照/记忆/提醒），只影响本次请求
        - ``on_text``：兼容旧回调，等价于消费 TextEvent
        - 同一实例的并发调用被锁串行化，消息不会交错
        """
        if hydrate and not self._conv.messages:
            self._conv.load_history(hydrate)
        if injected is not None:
            self.set_injected(injected)
        async with self._lock:
            async for event in self._run(
                user_message, system_prompt=system_prompt, on_text=on_text
            ):
                yield event

    # ── 内部：真正的循环 ──

    async def _run(
        self,
        user_message: str,
        *,
        system_prompt: str | None = None,
        on_text: Any = None,
    ) -> AsyncIterator[AgentEvent]:
        conv = self._conv
        conv.add_user(user_message)

        system = system_prompt or build_system_prompt()
        tool_specs = self._registry.specs()
        records: list[ToolCallRecord] = []
        steps: list[Step] = []
        usage: dict[str, int] = {}
        last_text = ""

        for turn in range(1, self._max_turns + 1):
            text_parts: list[str] = []
            pending_calls: list[tuple[str, str, dict[str, Any]]] = []
            stream_error: str | None = None
            stop_reason = ""

            t_llm = time.perf_counter()
            async for event in self._llm.stream(
                self._payload(system),
                system=None,          # system 已经在 payload 里，传输层不再重复拼
                tools=tool_specs or None,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
            ):
                if isinstance(event, TextDelta):
                    text_parts.append(event.text)
                    if on_text is not None:
                        on_text(event.text)
                    yield TextEvent(turn, event.text)
                elif isinstance(event, ThinkingDelta):
                    log.debug("thinking: %s", event.text[:200])
                    yield ThinkingEvent(turn, event.text)
                elif isinstance(event, ToolUseEnd):
                    pending_calls.append((event.id, event.name, event.arguments))
                elif isinstance(event, MessageStop):
                    stop_reason = event.stop_reason
                    if event.usage:
                        # 累加而不是覆盖 —— 一轮对话可能有多次 LLM 调用，
                        # 只看最后一次会严重低估成本
                        for key, value in event.usage.items():
                            usage[key] = usage.get(key, 0) + value
                elif isinstance(event, StreamError):
                    stream_error = event.message

            text = "".join(text_parts).strip()
            if text:
                last_text = text

            steps.append(
                Step(
                    turn=turn,
                    kind="llm",
                    latency_ms=int((time.perf_counter() - t_llm) * 1000),
                    is_error=bool(stream_error),
                    chars=len(text),
                )
            )

            if stream_error:
                log.warning("LLM 流错误 turn=%d: %s", turn, stream_error)
                yield ErrorEvent(stream_error)
                yield DoneEvent(
                    AgentResult(
                        text=last_text or f"模型调用失败：{stream_error}",
                        tool_calls=records,
                        turns=turn,
                        usage=usage,
                        error=stream_error,
                        steps=steps,
                    )
                )
                return

            # 模型没要求工具 → 本轮就是最终答复
            if not pending_calls:
                conv.add_assistant(text)
                yield DoneEvent(
                    AgentResult(
                        text=text,
                        tool_calls=records,
                        turns=turn,
                        usage=usage,
                        steps=steps,
                    )
                )
                return

            # 模型要求调工具：先把这一轮的 assistant 消息落进对话，
            # 否则下一步的 tool 消息找不到对应的 tool_calls（协议会 400）
            conv.add_assistant_tool_calls(text, pending_calls)

            for call_id, name, arguments in pending_calls:
                log.info("tool call: %s(%s)", name, arguments)
                yield ToolCallEvent(turn, call_id, name, arguments)

                t_tool = time.perf_counter()
                result = await self._registry.execute(name, arguments)
                steps.append(
                    Step(
                        turn=turn,
                        kind="tool",
                        name=name,
                        latency_ms=int((time.perf_counter() - t_tool) * 1000),
                        is_error=result.is_error,
                        chars=len(result.content),
                    )
                )
                conv.add_tool_result(call_id, result.content)
                records.append(
                    ToolCallRecord(
                        name=name,
                        arguments=arguments,
                        result=result.content,
                        is_error=result.is_error,
                    )
                )
                yield ToolResultEvent(turn, call_id, name, result.content, result.is_error)

            log.info(
                "turn %d done, tools=%s, ctx_chars=%d",
                turn,
                [r.name for r in records],
                conv.estimate_chars(),
            )

        # 到轮次上限：模型还在要工具，但我们必须收尾
        log.warning("达到最大轮次 %d，强制结束", self._max_turns)
        yield DoneEvent(
            AgentResult(
                text=last_text or "（这轮查询太久了，先说说目前看到的情况，需要更细的再说）",
                tool_calls=records,
                turns=self._max_turns,
                usage=usage,
                truncated=True,
                steps=steps,
            )
        )
