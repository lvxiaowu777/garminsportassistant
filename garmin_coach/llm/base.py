"""LLM 传输层的抽象 —— 把不同厂商的流式响应归一化成同一组事件。

- ``LLMClient``：只声明 ``stream()``，上层（Agent）只认它；
- 事件：TextDelta / ThinkingDelta / ToolUseStart / ToolUseDelta / ToolUseEnd / MessageStop；
- 异常：``LLMError`` 一族 —— 把各 SDK 自己的错误类型翻译过来，
  上层只需 ``except LLMError``，换厂商不用改。

分成这么多事件类型而不是直接返回字符串，是为了让 Agent 在流式过程中
能分别处理「正在想 / 正在说 / 正在决定调哪个工具」——面向前端是打字机
和工具进度，面向内部是不用等整包回来才动。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Union


# ── 统一异常 ──

class LLMError(Exception):
    """所有传输层错误的基类。"""


class AuthenticationError(LLMError):
    """API key 无效或缺失。"""


class RateLimitError(LLMError):
    """被限流；``retry_after`` 来自响应的 retry-after 头（秒）。"""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class NetworkError(LLMError):
    """连接失败、超时、DNS 等。"""


# ── 事件 ──

@dataclass(frozen=True)
class TextDelta:
    """正文增量。"""

    text: str


@dataclass(frozen=True)
class ThinkingDelta:
    """思考过程增量（DeepSeek reasoner / Claude thinking）。"""

    text: str


@dataclass(frozen=True)
class ToolUseStart:
    """模型开始请求调用一个工具（id 和 name 已就绪，可先显示 loading）。"""

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
