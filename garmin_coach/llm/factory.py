"""按配置造出具体的 LLM 客户端。

目前只有 OpenAI 兼容协议一个实现（覆盖 DeepSeek / StepFun / Ark）。
以后要接 Anthropic 原生协议，在这里加一个分支即可，上层 Agent 不用改 ——
这就是把传输层抽象出来的收益。
"""
from __future__ import annotations

from .base import LLMClient
from .config import ProviderConfig, get_provider
from .openai import OpenAIClient


def create_client(provider: ProviderConfig | None = None) -> LLMClient:
    cfg = provider or get_provider()
    return OpenAIClient(
        base_url=cfg.base_url,
        api_key=cfg.api_key,
        model=cfg.model,
    )
