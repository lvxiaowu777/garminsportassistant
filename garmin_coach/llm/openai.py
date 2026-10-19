"""OpenAI 兼容协议的流式客户端。

DeepSeek / StepFun / 火山 Ark 都走这套协议，所以一个实现覆盖三家。

关键点：**工具调用是分片吐的**。OpenAI 的 SSE 里 tool_calls 不是一次给全，
而是每帧给你一段 `function.arguments` 的 JSON 字符串碎片，索引靠 `index` 对齐。
本模块负责把这些碎片拼成完整对象，再以 ToolUseEnd 一次性交给上层 ——
这样 Agent 不用自己处理「半个 JSON」。
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from .base import (
    LLMClient,
    MessageStop,
    StreamError,
    StreamEvent,
    TextDelta,
    ThinkingDelta,
    ToolUseDelta,
    ToolUseEnd,
    ToolUseStart,
)


class OpenAIClient(LLMClient):
    """OpenAI 兼容的 `POST <base_url>` 流式客户端。

    ``base_url`` 用的是**完整端点**（例如 ``https://api.deepseek.com/chat/completions``），
    与旧 ``llm_client.py`` 里的 PROVIDERS 配置保持一致，避免再叠一层路径拼接。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 60.0,
    ) -> None:
        self._url = base_url
        self._api_key = api_key
        self._model = model
        self._timeout = timeout

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> AsyncIterator[StreamEvent]:
        if not self._api_key:
            yield StreamError(f"{self._model} 的 API key 未配置")
            return

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": (
                ([{"role": "system", "content": system}] if system else []) + messages
            ),
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
            # 让服务端在最后一行带上 token 用量，Agent 用它记成本
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        # index -> {"id", "name", "args"}，用于拼装分片的工具调用
        pending: dict[int, dict[str, str]] = {}
        started: set[int] = set()
        finish_reason = ""
        usage: dict[str, int] = {}

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                async with client.stream(
                    "POST", self._url, json=payload, headers=headers
                ) as resp:
                    if resp.status_code != 200:
                        body = (await resp.aread()).decode("utf-8", "replace")
                        yield StreamError(f"HTTP {resp.status_code}: {body[:500]}")
                        return

                    async for raw in resp.aiter_lines():
                        if not raw or not raw.startswith("data:"):
                            continue
                        data = raw[5:].strip()
                        if not data or data == "[DONE]":
                            continue
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            # 不中断整条流：厂商偶尔会插入非 JSON 的心跳行
                            continue

                        if chunk.get("usage"):
                            usage = {
                                k: v
                                for k, v in chunk["usage"].items()
                                if isinstance(v, int)
                            }

                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        choice = choices[0]
                        finish_reason = choice.get("finish_reason") or finish_reason
                        delta = choice.get("delta") or {}

                        reasoning = delta.get("reasoning_content")
                        if reasoning:
                            yield ThinkingDelta(reasoning)

                        content = delta.get("content")
                        if content:
                            yield TextDelta(content)

                        for tc in delta.get("tool_calls") or []:
                            idx = tc.get("index", 0)
                            slot = pending.setdefault(
                                idx, {"id": "", "name": "", "args": ""}
                            )
                            if tc.get("id"):
                                slot["id"] = tc["id"]

                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                slot["name"] = fn["name"]

                            call_id = slot["id"] or f"call_{idx}"
                            if idx not in started and slot["name"]:
                                started.add(idx)
                                yield ToolUseStart(call_id, slot["name"])

                            fragment = fn.get("arguments")
                            if fragment:
                                slot["args"] += fragment
                                yield ToolUseDelta(call_id, fragment)

        except httpx.HTTPError as e:
            yield StreamError(f"网络错误：{e.__class__.__name__}: {e}")
            return

        # 流结束：把拼好的工具调用逐个交出去
        for idx in sorted(pending):
            slot = pending[idx]
            call_id = slot["id"] or f"call_{idx}"
            arguments: dict[str, Any] = {}
            raw_args = slot["args"].strip()
            if raw_args:
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
