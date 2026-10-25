"""旧链路的结构化标记解析：`[RECORD]{...}[/RECORD]`、`[PLAN]{...}[/PLAN]`。

Agent 路径**不用**这两个函数（那边是直接调 `record_training` / `save_plan` 工具）。
留在这里的原因：语音和 WebSocket 还走旧单轮链路，而且 `/api/chat` 的回退路径也用它兜底。
等 B6（把语音/WS 切到 Agent）做完，这个文件可以整体删除。
"""
from __future__ import annotations

import json
import re

_RECORD_RE = re.compile(r"\[RECORD\](.*?)\[/RECORD\]", re.DOTALL)
_PLAN_RE = re.compile(r"\[PLAN\](.*?)\[/PLAN\]", re.DOTALL)


def _extract(reply: str, pattern: re.Pattern) -> tuple[str, dict | None]:
    m = pattern.search(reply)
    if not m:
        return reply, None
    payload = None
    try:
        payload = json.loads(m.group(1))
    except (json.JSONDecodeError, TypeError):
        pass
    return reply.replace(m.group(0), "").strip(), payload


def extract_training_record(reply: str) -> tuple[str, dict | None]:
    """从 AI 回复提取 [RECORD] 训练记录标记，返回 (干净回复, 记录 dict 或 None)。"""
    return _extract(reply, _RECORD_RE)


def extract_plan(reply: str) -> tuple[str, dict | None]:
    """从 AI 回复提取 [PLAN] 未来计划标记，返回 (干净回复, plan dict 或 None)。"""
    return _extract(reply, _PLAN_RE)
