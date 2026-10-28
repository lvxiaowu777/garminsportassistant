"""记忆管理 Agent：合并重复、冲突取新、过期删除。

为什么交给 LLM：这些判断是语义的——
「左膝疼」和「左膝盖不舒服」是同一条；
「目标半马 1:45」和「目标改成全马 3:30」是冲突，应该保留后者；
「上周感冒」过两周就该淡出。硬编码规则写不完。

约定：LLM 返回**最终应保留的完整列表**（不是 diff），落地时整表替换。
失败时保留原数据，绝不删用户记忆。
"""
from __future__ import annotations

import json
import logging

from .inject import humanize_age
from .store import KINDS, all_memories, replace_memories

log = logging.getLogger(__name__)

SYSTEM = """你是记忆管理器。给你一个用户的全部长期记忆，你要治理它们：
1. 合并重复/近义（同一件事只留一条，value 取更完整准确的说法）；
2. 冲突取新（目标的、约束的、伤病的，保留最近更新且更具体的那条）；
3. 删除过期或不再成立的（例如早已恢复的临时伤病）；
4. 没有问题的原样保留。

输出 JSON 数组（就是治理后的全部记忆），每项：
{"kind": "injury|goal|preference|constraint|plan_decision", "key": "短标识", "value": "一句话", "confidence": 0-1}
只输出 JSON，不要解释。"""


def _parse(raw: str) -> list[dict] | None:
    if not raw:
        return None
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if item.get("kind") not in KINDS:
            continue
        if not (item.get("key") or "").strip() or not (item.get("value") or "").strip():
            continue
        out.append({
            "kind": item["kind"],
            "key": str(item["key"]).strip()[:64],
            "value": str(item["value"]).strip(),
            "confidence": float(item.get("confidence") or 1.0),
        })
    return out


async def consolidate(user_id: int) -> dict:
    """治理该用户的记忆。返回 {before, after, changed}。"""
    memories = all_memories(user_id)
    result = {"before": len(memories), "after": len(memories), "changed": False}
    if len(memories) < 2:
        return result

    lines = [
        f"- [{m['kind']}] {m['key']}：{m['value']}（{humanize_age(m['updated_at'])}更新，置信度 {m.get('confidence', 1.0)}）"
        for m in memories
    ]
    prompt = "当前记忆：\n" + "\n".join(lines)

    try:
        from garmin_coach.agent.llmclient.base import StreamError, TextDelta
        from garmin_coach.agent.llmclient.factory import get_shared_client

        client = get_shared_client()
        parts: list[str] = []
        async for ev in client.stream(
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": prompt}],
            max_tokens=1000,
            temperature=0.0,
        ):
            if isinstance(ev, TextDelta):
                parts.append(ev.text)
            elif isinstance(ev, StreamError):
                log.warning("记忆治理 LLM 失败: %s", ev.message)
                return result
        merged = _parse("".join(parts).strip())
    except Exception as exc:  # noqa: BLE001
        log.warning("记忆治理异常: %s: %s", exc.__class__.__name__, exc)
        return result

    if merged is None:
        return result

    written = replace_memories(user_id, merged)
    if written < 0:
        return result
    result["after"] = written
    result["changed"] = True
    log.info("memory consolidated user=%s: %d → %d", user_id, len(memories), written)
    return result
