"""记忆注入：把 `user_memory` 序列化成模型能直接采信的一段。

几条设计约定：
- 标签统一 `[记忆]`（和注入的 `[数据]` 区分开）；
- 每条带**多久以前更新的**（模型据此判断新旧），以及置信度低的标记；
- 只注入 top-N（默认 8），不塞原文、不塞聊天记录；
- 读失败返回空列表，绝不影响对话。
"""
from __future__ import annotations

import datetime as dt

from .store import top_memories

TAG = "[记忆]"
DEFAULT_LIMIT = 8


def humanize_age(updated_at: str, now: dt.datetime | None = None) -> str:
    """'2026-10-25 17:00:00' → '今天' / '3 天前' / '2 个月前'。"""
    current = now or dt.datetime.now()
    try:
        ts = dt.datetime.strptime((updated_at or "").strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return "时间未知"
    days = (current.date() - ts.date()).days
    if days <= 0:
        return "今天"
    if days == 1:
        return "昨天"
    if days < 30:
        return f"{days} 天前"
    if days < 365:
        return f"{days // 30} 个月前"
    return f"{days // 365} 年前"


def format_memory_block(memories: list[dict]) -> str:
    """拼成注入块文本。"""
    if not memories:
        return ""
    lines = [f"{TAG} 以下是之前会话确认过的事实，直接采信，不要重复追问："]
    for m in memories:
        age = humanize_age(m.get("updated_at", ""))
        confidence = m.get("confidence")
        low = "（不太确定）" if isinstance(confidence, (int, float)) and confidence < 0.6 else ""
        lines.append(f"- {m['kind']}/{m['key']}（{age}更新{low}）：{m['value']}")
    return "\n".join(lines)


def build_injected(user_id: int, limit: int = DEFAULT_LIMIT) -> list[dict]:
    """会话开始时的注入块（Agent 的 ``injected`` 参数）。"""
    try:
        memories = top_memories(user_id, limit)
    except Exception:  # noqa: BLE001 - 记忆读失败不能影响对话
        return []
    block = format_memory_block(memories)
    if not block:
        return []
    return [{"role": "system", "content": block}]
