"""Agent 主循环 —— 一个**异步生成器**，边跑边吐事件。

核心还是那个循环：问模型 → 模型要么直接答，要么要求调工具 →
执行工具把结果塞回对话 → 再问一次。区别是现在每一步都 ``yield`` 出来：

```
async for ev in agent.run("今晚跑不跑"):
    if isinstance(ev, TextEvent): 前端追加文字
    elif isinstance(ev, ToolCallEvent): 显示"正在查天气…"
    elif isinstance(ev, DoneEvent): 收尾
```

想要「一次拿到最终结果」的调用方用 ``collect()``（消费完事件流返回 ``AgentResult``）。
落库和恢复在 CM 里完成，Agent 只负责循环和 trace。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator

from garmin_coach.agent.llmclient.base import (
    LLMClient,
    MessageStop,
    StreamError,
    TextDelta,
    ThinkingDelta,
    ToolUseEnd,
)
from garmin_coach.agent.tools.registry import ToolRegistry

from ..conversation import ConversationManager
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
    """Agent —— **组合根 + 核心 loop**。

    一个用户一个实例，由 ``AgentPool`` 持有，空闲 1 小时回收。

    它持有：``llm`` / ``registry`` / ``prompts`` / ``memory`` / ``tracer`` / ``conv``。
    **不持有 SessionManager，也不碰数据库**：落库和恢复都在 CM 里（CM 持有 Session）。

    ``run()`` 只做三件事：CM.load() → 循环（模型 ↔ 工具）→ trace。
    """

    def __init__(
        self,
        llm: LLMClient,
        registry: ToolRegistry,
        prompts: "PromptProvider",
        conversation: ConversationManager,
        *,
        user_id: int = 0,
        provider: str = "",
        memory: Any = None,
        tracer: Any = None,
        max_turns: int = 8,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> None:
        self._llm = llm
        self._registry = registry
        self._prompts = prompts
        # system 是稳定的（不含时间/记忆），构造时构建一次即可；
        # 以后 hook 要追加段落，就在这里改成每轮 build(extra=...)。
        self._system_prompt = prompts.build()
        self._conv = conversation
        self._memory = memory
        self._tracer = tracer
        self.user_id = user_id
        self.provider = provider
        self._max_turns = max_turns
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._lock = asyncio.Lock()
        self.turns = 0
        self.last_active = time.time()

    # ── 身份 / 生命周期 ──

    @property
    def session_id(self) -> str:
        return self._conv.session_id

    @property
    def conversation(self) -> ConversationManager:
        return self._conv

    @property
    def idle_seconds(self) -> float:
        return time.time() - self.last_active

    @property
    def is_busy(self) -> bool:
        """正在跑一轮（锁被持有）。回收时要跳过它。"""
        return self._lock.locked()

    def touch(self) -> None:
        self.last_active = time.time()

    def close(self) -> None:
        """Agent 被回收：关闭会话（CM 负责投记忆提炼任务）。"""
        self._conv.close()
        self._conv.clear()

    # ── 注入 ──

    async def _build_injected(self) -> list[dict[str, Any]]:
        """本轮上下文块：记忆 + 数据新鲜度。

        这里是**唯一来源**。以后加 hook（比赛倒计时、临时提醒），也在这里
        追加段落，而不是给调用方开一个覆盖口子。
        """
        if self._memory is None:
            return []
        try:
            return await self._memory.build_injection()
        except Exception as exc:  # noqa: BLE001 - 注入失败不能挡住对话
            log.warning("injection failed: user=%s: %s", self.user_id, exc)
            return []

    def _messages(self, injected: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """历史消息 → 本轮上下文（user）→ 本轮用户消息 → 工具往返。

        只负责 messages；system 由 ``self._system_prompt`` 单独传给传输层
        （每个协议自己去摊到 system 字段 / system message）。

        上下文插在**本轮那条用户消息之前**（不是插到队尾）：
        一轮里可能有多次工具往返，插队尾会变成"tool_result 后面又来一条 user"，
        语义也错位；插在本轮用户消息前，表达的是"用户说这句话时的
        时间 / 记忆 / 数据"。
        """
        context = self._context_message(injected)
        messages = self._conv.serialize()
        if not context:
            return messages

        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                return messages[:i] + [{"role": "user", "content": context}] + messages[i:]
        return messages + [{"role": "user", "content": context}]

    @staticmethod
    def _context_message(injected: list[dict[str, Any]]) -> str:
        """把时间 + 注入块拼成一条 user 消息的正文（没有内容就返回空串）。"""
        from ..prompts.base import time_context

        lines = [time_context()]
        for block in injected:
            text = (block.get("content") or "").strip()
            if text:
                lines.append(text)
        if len(lines) == 1:
            return ""
        return "## 当前上下文\n" + "\n".join(f"- {line}" if "\n" not in line else line for line in lines)

    # ── 对外：事件流 ──

    async def run(
        self,
        user_message: str,
        *,
        user_record: dict[str, Any] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """跑一轮，逐事件产出。

        - ``user_record``：落库时附带的字段（语音：msg_type / audio_url / duration_sec）
        """
        self._conv.load()
        injected_blocks = await self._build_injected()

        async with self._lock:
            t0 = time.perf_counter()
            # CM 负责「进内存 + 落库」（语音的 meta 一起透传）
            self._conv.add_user(user_message, **(user_record or {}))

            result: AgentResult | None = None
            partial: list[str] = []
            try:
                async for event in self._loop(injected=injected_blocks):
                    if isinstance(event, TextEvent):
                        partial.append(event.text)
                    elif isinstance(event, DoneEvent):
                        result = event.result
                    yield event
            finally:
                self.turns += 1
                self.touch()
                if result is not None:
                    # 最终回答已经由 _loop 里的 conv.add_assistant 落库
                    self._record_trace(result, int((time.perf_counter() - t0) * 1000))
                else:
                    # 异常 / 客户端断开：assistant 没写。落一条 type=error 占位
                    # （不进内存，resume 时也会被过滤）。
                    text = "".join(partial).strip()
                    note = "（这条回复没有生成完——可能是网络或模型中断，可以再发一次）"
                    self._conv.fail(f"{text}\n\n{note}" if text else note)

    async def collect(self, user_message: str, **kwargs: Any) -> AgentResult:
        """消费完事件流返回最终结果（非流式接口 / 评测用）。"""
        result: AgentResult | None = None
        async for event in self.run(user_message, **kwargs):
            if isinstance(event, DoneEvent):
                result = event.result
        return result or AgentResult(text="", turns=0)

    # ── trace ──

    def _record_trace(self, result: AgentResult, elapsed_ms: int) -> None:
        if self._tracer is None:
            return
        try:
            self._tracer(
                user_id=self.user_id,
                session_id=self.session_id,
                provider=self.provider,
                turns=result.turns,
                total_tokens=int(result.usage.get("total_tokens", 0)),
                latency_ms=elapsed_ms,
                error=result.error,
                steps=result.steps,
                tool_calls=result.tool_calls,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("trace record failed: %s: %s", self.session_id, exc)

    # ── 内部：真正的循环 ──

    async def _loop(
        self,
        *,
        injected: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        conv = self._conv
        injected = list(injected or [])
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
                self._messages(injected),
                system=self._system_prompt,
                tools=tool_specs or None,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
            ):
                if isinstance(event, TextDelta):
                    text_parts.append(event.text)
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
                conv.add_tool_result(call_id, result.content, result.is_error)
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
