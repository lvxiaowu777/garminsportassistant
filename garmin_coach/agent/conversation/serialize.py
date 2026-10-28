"""消息序列化：保证发给 LLM 的序列协议合法。"""
from __future__ import annotations

from typing import Any


def serialize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 messages 规整成可直接 JSON 化的协议合法形态（原 CM.serialize 的独立版）。

    - 相邻同角色合并（崩溃留下的 `user, user`）
    - assistant 带 tool_calls 但缺 tool 结果 → 降级为纯文本
    - 孤立 tool 消息丢弃；多工具按 tool_call_id 逐个配对
    - 不修改入参，返回副本
    """
    out: list[dict[str, Any]] = []
    pending_ids: set[str] = set()   # 已发出、还没收到结果的 tool_call_id

    for idx, msg in enumerate(messages):
        role = msg.get("role")
        content = (msg.get("content") or "").strip()

        # 工具结果：只保留属于「上一批 tool_calls」的，按 id 逐个消费
        if role == "tool":
            call_id = msg.get("tool_call_id")
            if call_id in pending_ids:
                item = {"role": "tool", "tool_call_id": call_id, "content": content}
                if msg.get("is_error"):
                    item["is_error"] = True
                out.append(item)
                pending_ids.discard(call_id)
            continue

        calls = msg.get("tool_calls") or []
        if role == "assistant" and calls:
            # 往后看紧邻的连续 tool 消息，必须覆盖全部 call_id，否则整批降级成纯文本
            need = {tc.get("id") for tc in calls}
            have: set[str] = set()
            j = idx + 1
            while j < len(messages) and messages[j].get("role") == "tool":
                have.add(messages[j].get("tool_call_id"))
                j += 1
            if not need <= have:
                if content:
                    out.append({"role": "assistant", "content": content})
                continue
            out.append({"role": "assistant", "content": msg.get("content"), "tool_calls": calls})
            pending_ids = set(need)
            continue

        if not content:
            continue
        if out and out[-1].get("role") == role and not out[-1].get("tool_calls"):
            out[-1]["content"] = f"{out[-1]['content']}\n{content}"
        else:
            out.append({"role": role, "content": content})

    return out

    # ── 写入 ──

