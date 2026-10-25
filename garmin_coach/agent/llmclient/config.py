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

    _ENV_FILE = Path(__file__).resolve().parent.parent.parent.parent / ".env"
    if _ENV_FILE.exists():
        load_dotenv(_ENV_FILE, override=False)
except ImportError:  # pragma: no cover
    pass


@dataclass(frozen=True)
class ProviderConfig:
    """一家 LLM 提供商的连接信息。

    ``protocol`` 决定用哪个客户端：``openai``（兼容协议，覆盖 DeepSeek/StepFun/Ark）
    或 ``anthropic``（Claude 原生消息格式）。
    """

    key: str
    display_name: str
    base_url: str
    model: str
    api_key: str
    max_tokens: int = 4096
    protocol: str = "openai"


def _build_providers() -> dict[str, ProviderConfig]:
    # base_url 是 **SDK base**：官方 SDK 自己会拼 /chat/completions 或 /v1/messages
    return {
        "deepseek": ProviderConfig(
            key="deepseek",
            display_name="DeepSeek",
            base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            model=os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
            api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
        ),
        "stepfun": ProviderConfig(
            key="stepfun",
            display_name="StepFun",
            base_url=os.environ.get("STEPFUN_BASE_URL", "https://api.stepfun.com/v1"),
            model=os.environ.get("STEPFUN_MODEL", "step-3.7-flash"),
            api_key=os.environ.get("STEPFUN_API_KEY", ""),
        ),
        "ark": ProviderConfig(
            key="ark",
            display_name="Ark",
            base_url=os.environ.get(
                "ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"
            ),
            model=os.environ.get("ARK_MODEL", "deepseek-v3-250324"),
            api_key=os.environ.get("ARK_API_KEY", ""),
        ),
        "claude": ProviderConfig(
            key="claude",
            display_name="Claude",
            base_url=os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
            model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929"),
            api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            max_tokens=int(os.environ.get("ANTHROPIC_MAX_TOKENS", "4096")),
            protocol="anthropic",
        ),
        # 兼容 Anthropic 协议的第三方端点（如 DeepSeek 的 /anthropic）
        "deepseek-anth": ProviderConfig(
            key="deepseek-anth",
            display_name="DeepSeek (Anthropic)",
            base_url=os.environ.get(
                "DS_ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic"
            ),
            model=os.environ.get("DS_ANTHROPIC_MODEL", "deepseek-flash"),
            api_key=os.environ.get("DS_ANTHROPIC_API_KEY", ""),
            max_tokens=int(os.environ.get("DS_ANTHROPIC_MAX_TOKENS", "4096")),
            protocol="anthropic",
        ),
    }


PROVIDERS: dict[str, ProviderConfig] = _build_providers()
DEFAULT_PROVIDER = "deepseek"


def get_provider(name: str | None = None) -> ProviderConfig:
    """按名字取 provider；没指定时用后端配置（``LLM_PROVIDER``）。

    注意：**不接受终端用户传 provider** —— 这是后端部署配置，不是用户偏好。
    名字不存在或没有 key 时自动退到第一个有 key 的。
    """
    from garmin_coach.settings import settings

    key = name or settings.llm_provider or DEFAULT_PROVIDER
    provider = PROVIDERS.get(key) or PROVIDERS[DEFAULT_PROVIDER]
    if provider.api_key:
        return provider
    for candidate in PROVIDERS.values():
        if candidate.api_key:
            return candidate
    return provider
