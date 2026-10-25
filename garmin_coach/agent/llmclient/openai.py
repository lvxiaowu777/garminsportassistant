"""OpenAI 兼容协议的流式客户端（官方 SDK）。

DeepSeek / StepFun / 火山 Ark 都走这套协议，所以一个实现覆盖三家。
结构对齐 MewCode：``Client(config)`` → ``stream(...)`` → 统一事件。

关键点：**工具调用是分片吐的**。SDK 的 delta 里 ``tool_calls`` 每帧只给
一段 ``function.arguments`` 片段，索引靠 ``index`` 对齐。这里负责拼成完整
对象，再以 ``ToolUseEnd`` 一次性交给上层 —— Agent 不用自己处理「半个 JSON」。
"""
from __future__ import annotations

from typing import Any, AsyncIterator

from .base import (
    AuthenticationError,
    LLMClient,
    LLMError,
    MessageStop,
    NetworkError,
    RateLimitError,
    StreamError,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    ToolUseDelta,
    ToolUseEnd,
    ToolUseStart,
)
from .config import ProviderConfig


def to_openai_tools(specs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """协议中立的工具定义 → OpenAI function calling 格式。"""
    if not specs:
        return []
    return [
        {
            "type": "function",
            "function": {
                "name": spec.get("name", ""),
                "description": spec.get("description", ""),
                "parameters": spec.get("parameters") or {"type": "object", "properties": {}},
            },
        }
        for spec in specs
    ]


class OpenAIClient(LLMClient):
    """基于 ``openai.AsyncOpenAI`` 的客户端。

    ``config.base_url`` 是 **SDK base**（如 ``https://api.deepseek.com``），
    不是完整端点；兼容旧配置里写成 ``.../chat/completions`` 的情况（自动裁掉）。
    """

    def __init__(self, config: ProviderConfig) -> None:
        if not config.api_key:
            raise AuthenticationError(f"{config.display_name} 的 API key 未配置")

        from openai import AsyncOpenAI

        base_url = config.base_url.rstrip("/")
        for suffix in ("/chat/completions", "/v1/chat/completions"):
            if base_url.endswith(suffix):
                base_url = base_url[: -len(suffix)]
                break

        self.model = config.model
        self._client = AsyncOpenAI(api_key=config.api_key, base_url=base_url or None)

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> AsyncIterator[StreamEvent]:
        from openai import (
            APIConnectionError,
            APIStatusError,
            AuthenticationError as OpenAIAuthError,
            RateLimitError as OpenAIRateLimitError,
        )

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": ([{"role": "system", "content": system}] if system else []) + messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = to_openai_tools(tools)
            payload["tool_choice"] = "auto"

        pending: dict[int, dict[str, str]] = {}
        started: set[int] = set()
        finish_reason = ""
        usage: dict[str, int] = {}

        try:
            stream = await self._client.chat.completions.create(**payload)
            async for chunk in stream:
                if chunk.usage:
                    usage = {
                        k: v
                        for k, v in chunk.usage.model_dump().items()
                        if isinstance(v, int)
                    }

                choices = chunk.choices or []
                if not choices:
                    continue
                choice = choices[0]
                finish_reason = choice.finish_reason or finish_reason
                delta = choice.delta

                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    yield ThinkingDelta(reasoning)

                if delta.content:
                    yield TextDelta(delta.content)

                for tc in delta.tool_calls or []:
                    idx = tc.index if tc.index is not None else 0
                    slot = pending.setdefault(idx, {"id": "", "name": "", "args": ""})
                    if tc.id:
                        slot["id"] = tc.id

                    fn = tc.function
                    if fn and fn.name:
                        slot["name"] = fn.name

                    call_id = slot["id"] or f"call_{idx}"
                    if idx not in started and slot["name"]:
                        started.add(idx)
                        yield ToolUseStart(call_id, slot["name"])

                    if fn and fn.arguments:
                        slot["args"] += fn.arguments
                        yield ToolUseDelta(call_id, fn.arguments)

        except OpenAIAuthError as e:
            yield StreamError(f"API key 无效：{e}")
            return
        except OpenAIRateLimitError as e:
            yield StreamError(f"被限流：{e}")
            return
        except APIConnectionError as e:
            yield StreamError(f"网络错误：{e.__class__.__name__}: {e}")
            return
        except APIStatusError as e:
            yield StreamError(f"HTTP {e.status_code}: {e.message}")
            return
        except LLMError as e:
            yield StreamError(str(e))
            return

        for idx in sorted(pending):
            slot = pending[idx]
            call_id = slot["id"] or f"call_{idx}"
            arguments: dict[str, Any] = {}
            raw_args = slot["args"].strip()
            if raw_args:
                import json

                try:
                    parsed = json.loads(raw_args)
                    if isinstance(parsed, dict):
                        arguments = parsed
                except json.JSONDecodeError:
                    yield StreamError(
                        f"工具 {slot['name']} 的入参不是合法 JSON：{raw_args[:200]}"
                    )
            yield ToolUseEnd(call_id, slot["name"], arguments)

        yield MessageStop(finish_reason or "stop", usage)
