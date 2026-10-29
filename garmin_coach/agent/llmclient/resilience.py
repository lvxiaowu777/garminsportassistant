"""LLM 流的韧性包装：空闲超时 + 首包前重试。

规则：
- **只有还没吐出任何事件**时才重试（否则重试会重复半句话）；
- 超时指的是"两个 chunk 之间超过 N 秒"，不是总时长；
- 4xx（除 429）不重试（参数/key 错了，重试也没用）。
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import AsyncIterator, Callable

from .base import StreamError, StreamEvent

log = logging.getLogger(__name__)

#: 两个 chunk 之间最多等多久（秒）
IDLE_TIMEOUT = float(os.environ.get("LLM_IDLE_TIMEOUT_SECONDS", "45"))
#: 首包前的重试次数
RETRIES = int(os.environ.get("LLM_RETRIES", "2"))


def _status_code(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


def _retryable(exc: BaseException) -> bool:
    status = _status_code(exc)
    if status is not None:
        return status == 429 or status >= 500
    name = type(exc).__name__
    return any(k in name for k in ("Connection", "Timeout", "RateLimit", "Overloaded"))


def describe(exc: BaseException) -> str:
    """异常 → 给前端看的一句话（保持和原来各类错误的文案一致）。"""
    name = type(exc).__name__
    status = _status_code(exc)

    if "Authentication" in name:
        return f"API key 无效：{exc}"
    if "RateLimit" in name:
        retry_after = getattr(exc, "retry_after", None)
        if retry_after:
            return f"被限流：请等 {retry_after}s"
        return "被限流：请稍后重试"
    if "Connection" in name or "Timeout" in name:
        return f"网络错误：{name}: {exc}"
    if status is not None:
        return f"HTTP {status}: {exc}"
    return f"LLM 调用失败：{name}: {exc}"


async def resilient_stream(
    factory: Callable[[], AsyncIterator[StreamEvent]],
    *,
    retries: int | None = None,
    idle_timeout: float | None = None,
) -> AsyncIterator[StreamEvent]:
    """把"创建流的工厂"包成：空闲超时 + 首包前退避重试。"""
    max_retries = RETRIES if retries is None else retries
    timeout = IDLE_TIMEOUT if idle_timeout is None else idle_timeout

    attempt = 0
    retry_after: float | None = None
    while True:
        emitted = False
        retry_after = None
        try:
            stream = factory()
            iterator = stream.__aiter__()
            while True:
                try:
                    event = await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
                except StopAsyncIteration:
                    return
                emitted = True
                yield event
        except asyncio.TimeoutError:
            error = StreamError(f"LLM 响应超时：{timeout:g} 秒没有新数据")
            retry_ok = True
        except Exception as exc:  # noqa: BLE001 - 统一转成事件
            error = StreamError(describe(exc))
            retry_ok = _retryable(exc)
            retry_after = getattr(exc, "retry_after", None)

        if emitted or not retry_ok or attempt >= max_retries:
            yield error
            return

        attempt += 1
        delay = min(2.0 ** attempt, 8.0)
        if retry_after:
            delay = max(delay, float(retry_after))
        log.warning("LLM 流失败，%ss 后重试（%d/%d）：%s", delay, attempt, max_retries, error.message)
        await asyncio.sleep(delay)
