"""Anthropic（Claude）原生协议客户端（官方 SDK）。

结构对齐 MewCode 的 ``AnthropicClient``：``Client(config)`` + ``stream()``
+ 统一事件，异常翻译成本项目自己的 ``LLMError`` 一族。

**和 OpenAI 兼容协议的四点差别**（也是这个文件存在的理由）：

1. ``system`` 是顶层参数，不是 messages 里的一条；
2. 消息 ``content`` 是 **block 列表**：``text`` / ``tool_use`` / ``tool_result``；
3. 工具 schema 是 ``{name, description, input_schema}``；
4. 流事件是 ``content_block_*``，工具入参走 ``input_json_delta``（分片，要自己拼）。

消息格式转换放在这里，``ConversationManager`` 保持协议中立。
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

from .resilience import resilient_stream
from .base import (
    AuthenticationError,
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
from .config import ProviderConfig

ANTHROPIC_VERSION = "2023-06-01"

_STOP_MAP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "pause_turn": "stop",
}


# ── 消息 / 工具格式转换（纯函数，可单测）──

def split_system(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """把 system 消息抽出来（Anthropic 要求它单独传）。

    多个 system（基础提示词 + 记忆块）按顺序用空行拼接。
    """
    systems: list[str] = []
    rest: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "system":
            content = (msg.get("content") or "").strip()
            if content:
                systems.append(content)
        else:
            rest.append(msg)
    return "\n\n".join(systems), rest


def _text_blocks(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": text}] if text else []


def to_anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI 形状的 messages → Anthropic 的 block 形状。

    - assistant 带 ``tool_calls`` → ``text`` block + 若干 ``tool_use`` block；
    - ``role=tool`` → 并入一条 **user** 消息的 ``tool_result`` block；
    - 连续相同角色合并（Anthropic 要求 user/assistant 交替）；
    - 空内容跳过。
    """
    out: list[dict[str, Any]] = []

    def push(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})

    for msg in messages:
        role = msg.get("role")
        content = (msg.get("content") or "").strip()

        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": msg.get("tool_call_id") or "",
                # Anthropic 对空 content 不友好，给个占位
                "content": content or "(无输出)",
            }
            if msg.get("is_error"):
                block["is_error"] = True
            push("user", [block])
            continue

        if role == "assistant":
            blocks = _text_blocks(content)
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                raw = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw) if isinstance(raw, str) else (raw or {})
                except json.JSONDecodeError:
                    args = {}
                blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id") or "",
                    "name": fn.get("name") or "",
                    "input": args if isinstance(args, dict) else {},
                })
            push("assistant", blocks)
            continue

        if role == "user":
            push("user", _text_blocks(content))

    # Anthropic 要求第一条消息是 user：恢复历史时截断可能正好落在 assistant 上
    # （比如只取最近 20 条，第一条是 assistant）。OpenAI 兼容端点容忍，Anthropic 直接 400。
    while out and out[0]["role"] != "user":
        out.pop(0)

    return out


def to_anthropic_tools(specs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """协议中立的工具定义 → Anthropic 工具格式。

    入参是 ``Tool.spec()`` 的形状（name / description / parameters），
    也兼容旧版 OpenAI 包装（``{"type":"function","function":{...}}``）。
    """
    if not specs:
        return []
    out: list[dict[str, Any]] = []
    for tool in specs:
        fn = tool.get("function") or tool
        out.append({
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters")
            or fn.get("input_schema")
            or {"type": "object", "properties": {}},
        })
    return out


def _mark_cache(payload: dict[str, Any]) -> None:
    """在最长稳定前缀上打 cache 断点：system 尾、tools 尾、最后一条 user 尾。

    Anthropic 是**显式**缓存（OpenAI 兼容那几家是服务端隐式前缀缓存）。
    打点不改内容，只加一个 ``cache_control`` 标记。
    """
    system_blocks = payload.get("system")
    if system_blocks:
        system_blocks[-1]["cache_control"] = {"type": "ephemeral"}

    tools = payload.get("tools")
    if tools:
        tools[-1]["cache_control"] = {"type": "ephemeral"}

    messages = payload.get("messages") or []
    for msg in reversed(messages):
        if msg.get("role") == "user" and msg.get("content"):
            blocks = msg["content"]
            if blocks:
                blocks[-1]["cache_control"] = {"type": "ephemeral"}
            break


class AnthropicClient(LLMClient):
    """基于 ``anthropic.AsyncAnthropic`` 的客户端。

    ``config.base_url`` 是 **SDK base**（``https://api.anthropic.com``），
    不是完整端点；写成 ``.../v1/messages`` 时自动裁掉。
    """

    def __init__(self, config: ProviderConfig) -> None:
        if not config.api_key:
            raise AuthenticationError(f"{config.display_name} 的 API key 未配置")

        from anthropic import AsyncAnthropic

        base_url = config.base_url.rstrip("/")
        if base_url.endswith("/v1/messages"):
            base_url = base_url[: -len("/v1/messages")]

        self.model = config.model
        self._client = AsyncAnthropic(api_key=config.api_key, base_url=base_url or None)

    async def _stream_once(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> AsyncIterator[StreamEvent]:
        import anthropic as _anthropic

        sys_text, rest = split_system(messages)
        if system:
            sys_text = f"{system}\n\n{sys_text}".strip() if sys_text else system

        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": to_anthropic_messages(rest),
        }
        # 注意：anthropic SDK 2.x 的 messages.stream() 已不接受 temperature
        # （签名里没有这个参数），所以这里不再传；要调采样只能走 extra_body。
        if sys_text:
            payload["system"] = [{"type": "text", "text": sys_text}]
        if tools:
            payload["tools"] = to_anthropic_tools(tools)
        _mark_cache(payload)

        current_tool_name = ""
        current_tool_id = ""
        json_accum = ""
        in_thinking = False

        try:
            async with self._client.messages.stream(**payload) as stream:
                async for event in stream:
                    # NOTE: 现在没开 extended thinking；如果以后开，
                    # thinking block（含 signature）必须原样带回下一条 assistant
                    # 消息，否则 Anthropic 对带 tool_use 的轮次会 400。
                    if event.type == "content_block_start":
                        block = event.content_block
                        if block.type == "thinking":
                            in_thinking = True
                        elif block.type == "tool_use":
                            current_tool_name = block.name
                            current_tool_id = block.id
                            json_accum = ""
                            yield ToolUseStart(current_tool_id, current_tool_name)

                    elif event.type == "content_block_delta":
                        delta = event.delta
                        if delta.type == "text_delta":
                            yield TextDelta(delta.text)
                        elif delta.type == "thinking_delta":
                            yield ThinkingDelta(delta.thinking)
                        elif delta.type == "input_json_delta":
                            json_accum += delta.partial_json
                            yield ToolUseDelta(current_tool_id, delta.partial_json)

                    elif event.type == "content_block_stop":
                        in_thinking = False
                        if current_tool_name:
                            try:
                                args = json.loads(json_accum) if json_accum else {}
                            except json.JSONDecodeError:
                                args = {}
                                yield StreamError(
                                    f"工具 {current_tool_name} 的入参不是合法 JSON：{json_accum[:200]}"
                                )
                            yield ToolUseEnd(current_tool_id, current_tool_name, args)
                            current_tool_name = ""
                            current_tool_id = ""
                            json_accum = ""

                final = await stream.get_final_message()
                usage = final.usage
                cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
                cache_creation = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
                # Anthropic 的 input_tokens **不含**缓存命中的部分，要把它们加回来，
                # 否则和 OpenAI 兼容那几家（prompt_tokens 含缓存）对不上，成本会算低。
                input_total = (
                    int(getattr(usage, "input_tokens", 0) or 0)
                    + cache_read
                    + cache_creation
                )
                usage_dict = {
                    "prompt_tokens": input_total,
                    "completion_tokens": int(getattr(usage, "output_tokens", 0) or 0),
                }
                usage_dict["total_tokens"] = (
                    usage_dict["prompt_tokens"] + usage_dict["completion_tokens"]
                )
                if cache_read:
                    usage_dict["prompt_cache_hit_tokens"] = cache_read

                yield MessageStop(_STOP_MAP.get(final.stop_reason or "", final.stop_reason or "stop"), usage_dict)

        except _anthropic.APIError:
            # 交给 resilience 统一格式化 + 决定是否重试
            raise

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ):
        """对外入口：超时 / 首包前重试由 resilience 负责。"""
        async for event in resilient_stream(
            lambda: self._stream_once(
                messages,
                system=system,
                tools=tools,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        ):
            yield event
