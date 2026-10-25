"""Agent 层事件与结果结构。

**为什么单独一层事件**：传输层（``llm/``）的事件是「厂商形状」的
（TextDelta / ToolUseEnd…），直接暴露给前端会把协议细节漏出去。
这里再封一层与厂商无关的领域事件，Web / Android / CLI 只消费这一层，
换模型或加流式都不用改展示端。

对应总纲 §2.10「两跳 SSE + 两层事件」的第二层。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ── 结果结构（不是事件，是一次 run 的最终产物）──

@dataclass
class ToolCallRecord:
    """一次工具调用留痕，用于日志和调试（不出现在用户回复里）。"""

    name: str
    arguments: dict[str, Any]
    result: str
    is_error: bool = False


@dataclass
class Step:
    """一次 LLM 调用或工具调用的耗时留痕（评测和 trace 表的原始数据）。

    这是**运行时观测**，不参与业务逻辑；写不写库由调用方决定。
    """

    turn: int
    kind: str            # "llm" | "tool"
    name: str = ""       # 工具名；llm 步骤为空
    latency_ms: int = 0
    is_error: bool = False
    chars: int = 0       # llm=正文增量字符数；tool=结果字符数


@dataclass
class AgentResult:
    text: str
    tool_calls: list[ToolCallRecord] = field(default_factory=list)
    turns: int = 1
    usage: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    truncated: bool = False
    steps: list[Step] = field(default_factory=list)

    @property
    def tools_used(self) -> list[str]:
        return [c.name for c in self.tool_calls]

    @property
    def llm_ms(self) -> int:
        """所有 LLM 调用的总耗时。"""
        return sum(s.latency_ms for s in self.steps if s.kind == "llm")

    @property
    def tool_ms(self) -> int:
        """所有工具调用的总耗时。"""
        return sum(s.latency_ms for s in self.steps if s.kind == "tool")


# ── 事件 ──

class AgentEvent:
    """所有 Agent 层事件的基类（仅用于 isinstance 归类）。"""

    __slots__ = ()


@dataclass(frozen=True)
class TextEvent(AgentEvent):
    """正文增量 —— 前端拿它做打字机。"""

    turn: int
    text: str


@dataclass(frozen=True)
class ThinkingEvent(AgentEvent):
    """模型的思考/推理增量（推理模型才有）。"""

    turn: int
    text: str


@dataclass(frozen=True)
class ToolCallEvent(AgentEvent):
    """模型决定调用一个工具（参数已拼成完整对象）。"""

    turn: int
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolResultEvent(AgentEvent):
    """工具执行完毕。"""

    turn: int
    call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class DoneEvent(AgentEvent):
    """一轮结束，携带完整结果。"""

    result: AgentResult


@dataclass(frozen=True)
class ErrorEvent(AgentEvent):
    """致命错误（前端可据此提示；``DoneEvent`` 仍会照常发出）。"""

    message: str


def to_sse(event: AgentEvent) -> dict[str, Any]:
    """把事件转成可直接 JSON 化的 dict（SSE/WS 都用这一份）。"""
    if isinstance(event, TextEvent):
        return {"type": "text", "text": event.text, "turn": event.turn}
    if isinstance(event, ThinkingEvent):
        return {"type": "thinking", "text": event.text, "turn": event.turn}
    if isinstance(event, ToolCallEvent):
        return {
            "type": "tool_call",
            "call_id": event.call_id,
            "name": event.name,
            "arguments": event.arguments,
            "turn": event.turn,
        }
    if isinstance(event, ToolResultEvent):
        return {
            "type": "tool_result",
            "call_id": event.call_id,
            "name": event.name,
            "content": event.content,
            "is_error": event.is_error,
            "turn": event.turn,
        }
    if isinstance(event, DoneEvent):
        return {
            "type": "done",
            "reply": event.result.text,
            "turns": event.result.turns,
            "tools": event.result.tools_used,
            "usage": event.result.usage,
            "truncated": event.result.truncated,
            "error": event.result.error,
        }
    if isinstance(event, ErrorEvent):
        return {"type": "error", "message": event.message}
    return {"type": "unknown"}
