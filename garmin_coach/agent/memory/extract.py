"""记忆提炼：从一段对话里抽出结构化事实。

两条路：
- ``extract_with_llm``：一次 LLM 调用（预定义提示词），产出
  ``[{kind, key, value, confidence}]``；
- ``extract_with_rules``：关键词兜底，LLM 不可用时也能抓明显事实。

为什么提炼要离线做：放在请求路径里会把"恢复会话"从 50ms 变成秒级，
而且失败会传导给用户（见总纲 §2.9）。
"""
from __future__ import annotations

import json
import logging
import re

log = logging.getLogger(__name__)

KINDS = ("injury", "goal", "preference", "constraint", "plan_decision")

SYSTEM = """你是运动教练的记忆提炼器。从对话里抽取**值得长期记住**的事实。
只输出 JSON 数组，不要解释。每项格式：
{"kind": "injury|goal|preference|constraint|plan_decision", "key": "短标识", "value": "一句话事实", "confidence": 0.0-1.0}

规则：
- 只抽用户明确表达的事实（伤病、目标、偏好、约束、计划调整），不要抽寒暄和问句；
- key 要短且稳定：伤病用部位（"左膝"），目标用"目标"，偏好用主题（"间歇跑"）；
- 同一件事不要拆成多条；
- 没有可抽的就输出 []。"""


def _model_text(messages: list[dict]) -> str:
    lines = []
    for m in messages:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            lines.append(f"{'用户' if role == 'user' else '教练'}：{content}")
    return "\n".join(lines)


async def extract_with_llm(messages: list[dict]) -> list[dict]:
    """调用一次 LLM 抽事实；失败返回空列表（调用方会退到规则版）。"""
    text = _model_text(messages)
    if not text:
        return []

    try:
        from garmin_coach.agent.llmclient.base import StreamError, TextDelta
        from garmin_coach.agent.llmclient.factory import get_shared_client

        client = get_shared_client()
        parts: list[str] = []
        async for ev in client.stream(
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": text}],
            max_tokens=800,
            temperature=0.0,
        ):
            if isinstance(ev, TextDelta):
                parts.append(ev.text)
            elif isinstance(ev, StreamError):
                log.warning("记忆提炼 LLM 失败: %s", ev.message)
                return []
        raw = "".join(parts).strip()
        items = _parse_json_array(raw)
        return [x for x in items if _valid(x)]
    except Exception as exc:  # noqa: BLE001
        log.warning("记忆提炼异常: %s: %s", exc.__class__.__name__, exc)
        return []


def _parse_json_array(raw: str) -> list[dict]:
    """宽容解析：模型可能带 ```json 围栏或前后废话。"""
    if not raw:
        return []
    start = raw.find("[")
    end = raw.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return []
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


def _valid(item: dict) -> bool:
    return (
        item.get("kind") in KINDS
        and bool((item.get("key") or "").strip())
        and bool((item.get("value") or "").strip())
    )


# ── 规则兜底 ──

_BODY = r"(左|右)?(膝盖|膝|髌骨|脚踝|跟腱|小腿|大腿|腰|肩|肘|腕|髋|足底|脚)"
_PAIN = r"(不舒服|不适|疼|痛|伤)"

_RULES: list[tuple[re.Pattern[str], str, object]] = [
    (re.compile(rf"{_BODY}[^，。;；,.]{{0,8}}{_PAIN}"), "injury",
     lambda m: f"{m.group(1) or ''}{m.group(2)}"),
    (re.compile(r"(目标|想跑|打算跑|PB|pb)[^，。;；,.]{0,12}?(半马|全马|\d+\s*公里)\s*(\d+[:：]\d+)?|(目标成绩)\s*(\d+[:：]\d+)"),
     "goal", lambda m: "目标"),
    (re.compile(r"(不喜欢|讨厌|不想|受不了)([^，。;；,.]{0,12})"), "preference",
     lambda m: (m.group(2) or "偏好").strip()[:16]),
    (re.compile(r"(只能|最多|不能|没时间)([^，。;；,.]{0,12})(跑|练|训练)"), "constraint",
     lambda m: "时间约束"),
    (re.compile(r"(改成|换成|调整为)([^，。;；,.]{0,16})"), "plan_decision",
     lambda m: "计划调整"),
]


def extract_with_rules(text: str) -> list[dict]:
    found: list[dict] = []
    for pattern, kind, key_fn in _RULES:
        m = pattern.search(text or "")
        if not m:
            continue
        try:
            key = key_fn(m)  # type: ignore[operator]
        except Exception:  # noqa: BLE001
            continue
        found.append({
            "kind": kind,
            "key": (key or kind)[:64],
            "value": m.group(0).strip(),
            "confidence": 0.8,
        })
    return found
