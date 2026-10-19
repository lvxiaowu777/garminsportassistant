"""LLM 传输层的抽象 —— 把不同厂商的流式响应归一化成同一组事件。

设计参照 MewCode 的 ``mewcode/client.py``：上层（Agent）不关心 SSE 长什么样、
不关心是 OpenAI 还是 Anthropic 协议，只消费下面这几种事件。

分成这么多事件类型而不是直接返回一个字符串，是为了让 Agent 能在流式过程中
把「模型正在想 / 正在说 / 正在决定调哪个工具」这些中间态分别处理 ——
面向前端就是打字机效果和工具调用进度，面向内部就是不用等整包回来才动。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Union


@dataclass(frozen=True)
class TextDelta:
    """正文增量。"""

    text: str


@dataclass(frozen=True)
class ThinkingDelta:
    """思考过程增量（DeepSeek reasoner 等推理模型会吐这个）。"""

    text: str


@dataclass(frozen=True)
class ToolUseStart:
    """模型开始请求调用一个工具。"""

    id: str
    name: str


@dataclass(frozen=True)
class ToolUseDelta:
    """工具入参的 JSON 片段 —— 厂商是分片吐的，需要上层自己拼。"""

    id: str
    partial_json: str


@dataclass(frozen=True)
class ToolUseEnd:
    """工具入参完整，可以执行了。"""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class MessageStop:
    """本轮结束。stop_reason 常见值：stop / length / tool_calls。"""

    stop_reason: str
    usage: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class StreamError:
    """传输层或协议层错误。不抛异常，作为事件返回，让 Agent 决定怎么兜。"""

    message: str


StreamEvent = Union[
    TextDelta,
    ThinkingDelta,
    ToolUseStart,
    ToolUseDelta,
    ToolUseEnd,
    MessageStop,
    StreamError,
]


class LLMClient(ABC):
    """一次对话补全的传输接口。

    职责边界：只负责「把请求发出去、把流解析成 StreamEvent」。
    **不负责**对话历史管理（那是 ConversationManager），
    **也不负责**工具执行（那是 ToolRegistry + Agent）。
    """

    @abstractmethod
    def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> AsyncIterator[StreamEvent]:
        """发起一次流式补全，逐个产出事件。"""
        raise NotImplementedError
