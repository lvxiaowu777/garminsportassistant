"""提示词注入的确定性拦截（不依赖模型自觉）。

只做一件事：用户消息里出现"伪造系统指令 / 越狱 / 套 prompt"的模式时，
在进模型之前就拒掉。宁可误杀一句，也不让人把 demo 当免费 LLM。
"""
from __future__ import annotations

import re

#: 命中即拒绝（中文 + 英文常见越狱话术）
PATTERNS: list[tuple[str, str]] = [
    (r"忽略.{0,8}(之前|以上|前面|所有).{0,8}(指令|对话|规则|设定)", "试图覆盖系统规则"),
    (r"(系统|开发者).{0,4}(指令|提示词|prompt)", "伪造系统指令"),
    (r"最高优先级", "伪造优先级"),
    (r"你现在(是|扮演)|从现在开始你(是|要)", "试图改写身份"),
    (r"(ignore|disregard).{0,20}(previous|above|prior).{0,20}(instruction|prompt|rule)", "试图覆盖系统规则"),
    (r"(system|developer)\s*(prompt|instruction)", "套取系统提示词"),
    (r"(越狱|jailbreak|DAN|开发者模式|无限制模式)", "越狱话术"),
    (r"(输出|告诉我|复述).{0,8}(你的)?(系统提示|system prompt|初始指令)", "套取系统提示词"),
    (r"不要(遵守|管|理会).{0,8}(规则|限制|设定)", "试图绕过规则"),
]

_COMPILED = [(re.compile(p, re.IGNORECASE), reason) for p, reason in PATTERNS]


def find_injection(text: str) -> str | None:
    """命中返回原因，否则 None。"""
    raw = (text or "").strip()
    if not raw:
        return None
    for pattern, reason in _COMPILED:
        if pattern.search(raw):
            return reason
    return None


def rejection_message(reason: str) -> str:
    return (
        f"这条消息看起来像是在改写我的规则（{reason}），我不能照做。"
        "我是你的运动教练，只聊训练、恢复和身体数据——要问这些随时说。"
    )
