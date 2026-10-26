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
from ..prompts import PromptProvider
from ..session.store import Session

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

    它持有：
    - ``llm``：传输层客户端（按 provider 共享）
    - ``registry``：工具表（绑定 user_id）
    - ``prompts``：提示词提供者（依赖注入）
    - ``sessions``：会话管理器（拿 Session、落库、恢复）
    - ``memory``：长期记忆（注入 + 提炼）
    - ``tracer``：运行轨迹（agent_runs / agent_steps）
    - ``conv``：对话管理器（跟着 Agent 活，1 小时内的多轮共用）

    ``run()`` 是唯一入口：落 user → 循环（模型 ↔ 工具）→ 落 assistant → trace。
    """

    def __init__(
        self,
        llm: LLMClient,
        registry: ToolRegistry,
        prompts: "PromptProvider",
        sessions: "SessionManager",
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
        self._sessions = sessions
        self._memory = memory
        self._tracer = tracer
        self.user_id = user_id
        self.provider = provider
        self._max_turns = max_turns
        self._temperature = temperature
        self._max_tokens = max_tokens
        # 构造时就绑定会话：Agent 是会话的持有者，session_id 立刻可用
        self._session: Session = sessions.open(user_id)
        self._conv = ConversationManager(user_id=user_id, session_id=self._session.session_id)
        self._lock = asyncio.Lock()
        self._resumed = False
        self._injected: list[dict[str, Any]] = []
        self.turns = 0
        self.last_active = time.time()

    # ── 身份 / 生命周期 ──

    @property
    def session_id(self) -> str:
        return self._session.session_id

    @property
    def conversation(self) -> ConversationManager:
        return self._conv

    @property
    def idle_seconds(self) -> float:
        return time.time() - self.last_active

    def touch(self) -> None:
        self.last_active = time.time()

    def detach_session(self) -> None:
        """只断开引用、不触发 close（清空历史 / 评测重置时用）。"""
        self._session = self._sessions.open(self.user_id)
        self._conv = ConversationManager(
            user_id=self.user_id, session_id=self._session.session_id
        )
        self._resumed = False

    def close(self) -> None:
        """Agent 被回收：关闭会话（投记忆提炼任务）。"""
        if self._session is not None:
            self._session.close()

    # ── 会话（构造时已绑定，历史只恢复一次）──

    def _ensure_session(self) -> Session:
        if not self._resumed:
            self._resumed = True
            history = self._session.resume()
            if history:
                self._conv.load_history(history)
        self._session.touch()
        return self._session

    # ── 注入 ──

    def set_injected(self, injected: list[dict[str, Any]] | None) -> None:
        """覆盖本轮注入块（测试用；默认由 memory 构建）。"""
        self._injected = list(injected or [])

    async def _build_injected(self) -> list[dict[str, Any]]:
        if self._injected:
            return self._injected
        if self._memory is None:
            return []
        try:
            return await self._memory.build_injection()
        except Exception as exc:  # noqa: BLE001 - 注入失败不能挡住对话
            log.warning("injection failed: user=%s: %s", self.user_id, exc)
            return []

    def _payload(self, system: str) -> list[dict[str, Any]]:
        """system → 注入块 → 会话消息（直接可 JSON 化）。"""
        payload: list[dict[str, Any]] = [{"role": "system", "content": system}]
        payload.extend(self._injected)
        payload.extend(self._conv.serialize())
        return payload

    # ── 对外：事件流 ──

    async def run(
        self,
        user_message: str,
        *,
        persist_user: bool = True,
        injected: list[dict[str, Any]] | None = None,
        on_text: Any = None,
    ) -> AsyncIterator[AgentEvent]:
        """跑一轮，逐事件产出。

        - ``persist_user=False``：调用方已经写过 user 消息（语音带 audio_url）
        - ``injected``：覆盖本轮注入块；不传则用 memory 构建
        """
        session = self._ensure_session()
        self._injected = list(injected) if injected is not None else []
        injected_blocks = await self._build_injected()

        async with self._lock:
            t0 = time.perf_counter()
            self._conv.add_user(user_message)
            if persist_user:
                session.append("user", user_message)

            result: AgentResult | None = None
            try:
                async for event in self._loop(
                    system_prompt=None,
                    injected=injected_blocks,
                    on_text=on_text,
                ):
                    if isinstance(event, DoneEvent):
                        result = event.result
                    yield event
            finally:
                self.turns += 1
                self.touch()
                session.turns += 1
                session.touch()
                if result is not None:
                    session.append("assistant", result.text)
                    self._record_trace(result, int((time.perf_counter() - t0) * 1000))

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
        system_prompt: str | None = None,
        injected: list[dict[str, Any]] | None = None,
        on_text: Any = None,
    ) -> AsyncIterator[AgentEvent]:
        conv = self._conv
        self._injected = list(injected or [])
        system = system_prompt or self._prompts.build()
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
