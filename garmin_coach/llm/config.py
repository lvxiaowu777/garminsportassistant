"""Provider 配置 —— 从环境变量读取，不再把 key 写进代码。

和旧 ``llm_client.py`` 的 PROVIDERS 保持同样的地址与模型名，只是把
「配置、提示词、HTTP 适配、ASR」拆开了：这个模块只负责配置。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# 加载项目根目录的 .env（不依赖调用方先 export 环境变量）
try:  # dotenv 是可选依赖，缺席时退回「只读真实环境变量」
    from dotenv import load_dotenv

    _ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"
    if _ENV_FILE.exists():
        load_dotenv(_ENV_FILE, override=False)
except ImportError:  # pragma: no cover
    pass


@dataclass(frozen=True)
class ProviderConfig:
    """一家 LLM 提供商的连接信息。"""

    key: str
    display_name: str
    base_url: str
    model: str
    api_key: str
    max_tokens: int = 4096


def _build_providers() -> dict[str, ProviderConfig]:
    return {
        "deepseek": ProviderConfig(
            key="deepseek",
            display_name="DeepSeek",
            base_url="https://api.deepseek.com/chat/completions",
            model=os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
            api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
        ),
        "stepfun": ProviderConfig(
            key="stepfun",
            display_name="StepFun",
            base_url="https://api.stepfun.com/v1/chat/completions",
            model=os.environ.get("STEPFUN_MODEL", "step-3.7-flash"),
            api_key=os.environ.get("STEPFUN_API_KEY", ""),
        ),
        "ark": ProviderConfig(
            key="ark",
            display_name="Ark",
            base_url="https://ark.cn-beijing.volces.com/api/v3/chat/completions",
            model=os.environ.get("ARK_MODEL", "deepseek-v3-250324"),
            api_key=os.environ.get("ARK_API_KEY", ""),
        ),
    }


PROVIDERS: dict[str, ProviderConfig] = _build_providers()
DEFAULT_PROVIDER = "deepseek"


def get_provider(name: str | None = None) -> ProviderConfig:
    """按名字取 provider；没指定或名字不存在时用默认，且自动跳过没有 key 的。"""
    key = name or DEFAULT_PROVIDER
    provider = PROVIDERS.get(key) or PROVIDERS[DEFAULT_PROVIDER]
    if provider.api_key:
        return provider
    for candidate in PROVIDERS.values():
        if candidate.api_key:
            return candidate
    return provider
