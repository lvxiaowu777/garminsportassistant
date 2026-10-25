"""按配置造出具体的 LLM 客户端（对应 MewCode 的 ``create_client``）。

协议由 ``ProviderConfig.protocol`` 决定：
- ``openai``（默认）：覆盖 DeepSeek / StepFun / Ark 等兼容协议；
- ``anthropic``：Claude 原生消息格式。

**客户端是共享的**：同一个 (protocol, base_url, model) 只造一个实例，
所有会话复用它 —— SDK 内部的 httpx 连接池按实例算，每个会话一个会白建连接。
"""
from __future__ import annotations

from .anthropic import AnthropicClient
from .base import LLMClient
from .config import ProviderConfig, get_provider
from .openai import OpenAIClient

_shared: dict[tuple[str, str, str], LLMClient] = {}


def create_client(provider: ProviderConfig | None = None) -> LLMClient:
    """每次新建（测试、临时换配置用）。生产路径请用 ``get_shared_client``。"""
    cfg = provider or get_provider()
    if cfg.protocol == "anthropic":
        return AnthropicClient(cfg)
    return OpenAIClient(cfg)


def get_shared_client(provider: ProviderConfig | None = None) -> LLMClient:
    """按 (protocol, base_url, model) 缓存客户端。"""
    cfg = provider or get_provider()
    key = (cfg.protocol, cfg.base_url, cfg.model)
    client = _shared.get(key)
    if client is None:
        client = create_client(cfg)
        _shared[key] = client
    return client


def reset_shared_clients() -> None:
    """测试用：清空缓存。"""
    _shared.clear()
