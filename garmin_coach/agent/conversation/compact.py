"""上下文压缩（对齐 DSH 的 compaction-basic，简化版）。

规则：
- 触发：估算 token 超过 ``COMPACT_TRIGGER_TOKENS``（默认 30k）；
- 保留：最近 ``KEEP_MESSAGES`` 条（默认 5），并把切点对齐到 user 消息，
  避免切开 assistant(tool_calls) / tool 的配对；
- 产物：一条带 ``<compact_boundary>`` 的 user 消息（摘要 + "__ 保留消息在它后面）；
- 持久化：``chat_history.type='compact'``，恢复时从最后一条 compact 开始加载。
"""
from __future__ import annotations

import os
from typing import Any, Awaitable, Callable

BOUNDARY_OPEN = "<compact_boundary>"
BOUNDARY_CLOSE = "</compact_boundary>"

#: 超过多少 token 触发压缩（按预算定，不按模型窗口）
COMPACT_TRIGGER_TOKENS = int(os.environ.get("COMPACT_TRIGGER_TOKENS", "30000"))
#: 压缩后保留多少条最近消息
KEEP_MESSAGES = int(os.environ.get("COMPACT_KEEP_MESSAGES", "5"))
#: 至少多少条才值得压缩
MIN_MESSAGES = 12

COMPACT_PROMPT = """你是对话压缩器。把上面的对话浓缩成一份结构化检查点，让另一个模型能无损接续工作。

严格输出下面结构（没有内容就写"(无)"，不要省略任何小节）：

## 用户诉求
## 已确认的事实
## 工具查询结果
## 未完成的事
## 下一步

规则：
- 中文，短句，条目化；
- 保留精确数字、日期、指标名、用户明确纠正过的说法；
- 不要提"这是压缩结果"，不要调用任何工具。"""


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """粗略估算 token（中文约 1 字 ≈ 0.6 token；英文约 4 字符 ≈ 1 token）。"""
    chars = 0
    for message in messages:
        content = message.get("content") or ""
        if isinstance(content, str):
            chars += len(content)
        for call in message.get("tool_calls") or []:
            chars += len(str(call.get("function", {}).get("arguments", "")))
    return int(chars / 1.7)


def plan_cut(messages: list[dict[str, Any]], keep: int = KEEP_MESSAGES) -> int | None:
    """返回切点下标：保留 ``messages[cut:]``；无需压缩返回 None。

    切点必须落在 user 消息上——既满足 Anthropic 首条为 user 的要求，
    也天然不会把 assistant(tool_calls) 和它的 tool 结果切开。
    """
    if len(messages) <= max(MIN_MESSAGES, keep + 2):
        return None
    if estimate_tokens(messages) < COMPACT_TRIGGER_TOKENS:
        return None

    cut = len(messages) - keep
    while cut < len(messages) and messages[cut].get("role") != "user":
        cut += 1
    return cut if cut < len(messages) else None


def build_boundary(summary: str) -> str:
    """压缩后的上下文消息正文。"""
    return f"{BOUNDARY_OPEN}\n{summary.strip()}\n{BOUNDARY_CLOSE}"


async def default_summarize(
    messages: list[dict[str, Any]],
    call_llm: Callable[[list[dict[str, Any]], str], Awaitable[str]],
) -> str:
    """把待压缩的消息交给 LLM，返回摘要文本。"""
    return await call_llm(messages, COMPACT_PROMPT)
