"""语音转文字（StepFun ASR）。

从 ``src/llm_client.py`` 抽出来的：ASR 不属于 LLM 对话链路，
语音接进 Agent 之后也要单独留下，所以给它一个自己的模块。

配置复用 ``llm/config.py`` 的 ``stepfun`` provider（读 ``STEPFUN_API_KEY``）。
"""
from __future__ import annotations

import logging
from pathlib import Path

import httpx

from garmin_coach.agent.llmclient.config import get_provider

log = logging.getLogger(__name__)

ASR_ENDPOINT = "https://api.stepfun.com/v1/audio/transcriptions"
ASR_MODEL = "stepaudio-2.5-asr"

_MIME = {
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".webm": "audio/webm",
    ".ogg": "audio/ogg",
}


async def asr_transcribe(audio_bytes: bytes, filename: str = "audio.m4a") -> str:
    """调 StepFun ASR 转写音频，返回文字（失败返回空字符串）。

    失败不抛异常：语音链路的调用方会把它当成"没听清"提示用户重说，
    而不是 500。
    """
    api_key = get_provider("stepfun").api_key
    if not api_key:
        log.warning("STEPFUN_API_KEY 未配置，ASR 不可用")
        return ""

    mime = _MIME.get(Path(filename).suffix.lower(), "audio/m4a")
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                ASR_ENDPOINT,
                headers={"Authorization": f"Bearer {api_key}"},
                data={"model": ASR_MODEL, "response_format": "json", "language": "zh"},
                files={"file": (filename, audio_bytes, mime)},
            )
            if resp.status_code != 200:
                log.warning("ASR 失败: HTTP %s %s", resp.status_code, resp.text[:200])
                return ""
            return resp.json().get("text", "")
    except Exception as exc:  # noqa: BLE001 - ASR 失败不该中断请求
        log.warning("ASR 异常: %s: %s", exc.__class__.__name__, exc)
        return ""
