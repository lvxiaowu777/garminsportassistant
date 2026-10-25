"""按配置造出具体的 LLM 客户端（对应 MewCode 的 ``create_client``）。

协议由 ``ProviderConfig.protocol`` 决定：
- ``openai``（默认）：覆盖 DeepSeek / StepFun / Ark 等兼容协议；
- ``anthropic``：Claude 原生消息格式。

**客户端是共享的**：同一个 (protocol, base_url, model) 只造一个实例，
所有会话复用它 —— SDK 内部的 httpx 连接池按实例算，每个会话一个会白建连接。
"""
from __future__ import annotations

import asyncio

from .anthropic import AnthropicClient
from .base import LLMClient
from .config import ProviderConfig, get_provider
from .openai import OpenAIClient

_shared: dict[tuple, LLMClient] = {}


def create_client(provider: ProviderConfig | None = None) -> LLMClient:
    """每次新建（测试、临时换配置用）。生产路径请用 ``get_shared_client``。"""
    cfg = provider or get_provider()
    if cfg.protocol == "anthropic":
        return AnthropicClient(cfg)
    return OpenAIClient(cfg)


def get_shared_client(provider: ProviderConfig | None = None) -> LLMClient:
    """按 (protocol, base_url, model, 事件循环) 缓存客户端。

    **为什么要带事件循环**：SDK 内部的 httpx.AsyncClient 绑定在创建它的
    loop 上。多 loop 场景（测试里每个请求一个新 loop、或一个进程里跑多个
    loop）复用同一个实例会报 ``Event loop is closed``。带上 loop id 后，
    每个 loop 各有一份连接池，生产环境（单 loop）仍然只有一个实例。
    """
    cfg = provider or get_provider()
    try:
        loop_id = id(asyncio.get_running_loop())
    except RuntimeError:  # 没有运行中的 loop（同步上下文）
        loop_id = 0

    key = (cfg.protocol, cfg.base_url, cfg.model, loop_id)
    client = _shared.get(key)
    if client is None:
        client = create_client(cfg)
        _shared[key] = client
    return client


def reset_shared_clients() -> None:
    """测试用：清空缓存。"""
    _shared.clear()
