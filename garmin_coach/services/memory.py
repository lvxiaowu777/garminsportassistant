"""长期记忆：写入、读取、注入、规则化提炼。

分工（对应总纲 §2.9）：

- **会话消息**（chat_history）：原话，7 天后删，用于展示和恢复；
- **长期记忆**（user_memory）：从会话里提炼的结论，长期保留、按 (kind,key) 覆盖；
- **outbox**：会话结束时投一条待办，离线任务消费后写记忆。

本模块只依赖 ``src/database.py`` 的会话/引擎，规则版提炼先跑通管道，
以后换成 LLM 提炼只改 ``extract_from_messages`` 的实现。
"""
from __future__ import annotations

import datetime as dt
import json
import re

from garmin_coach.db.database import SessionLocal
from garmin_coach.db.models import MemoryOutbox, UserMemory
from sqlalchemy import select

#: 允许的 kind（和总纲 §2.9 一致）
KINDS = ("injury", "goal", "preference", "constraint", "plan_decision")

#: 身体部位（带左右）——伤病的 key 用它，保证"左膝"和"右肩"不会互相覆盖
_BODY = r"(左|右)?(膝盖|膝|髌骨|脚踝|跟腱|小腿|大腿|腰|肩|肘|腕|髋|足底|脚)"
_PAIN = r"(不舒服|不适|疼|痛|伤)"

#: 规则版提炼：(正则, kind, key 取值函数)。宁可少抽，不要乱抽。
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


def now_str() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ── 写入 ──

def upsert_memory(
    user_id: int,
    kind: str,
    key: str,
    value: str,
    *,
    confidence: float = 1.0,
    source_session: str | None = None,
) -> bool:
    """写一条记忆；同 (user_id, kind, key) 覆盖。失败返回 False（不抛）。"""
    if kind not in KINDS:
        raise ValueError(f"未知 kind: {kind}（允许：{KINDS}）")
    value = (value or "").strip()
    if not value:
        return False

    session = SessionLocal()
    try:
        row = session.execute(
            select(UserMemory).where(
                UserMemory.user_id == user_id,
                UserMemory.kind == kind,
                UserMemory.key == key,
            )
        ).scalars().first()
        if row:
            row.value = value
            row.confidence = confidence
            row.source_session = source_session or row.source_session
            row.updated_at = now_str()
        else:
            session.add(UserMemory(
                user_id=user_id, kind=kind, key=key, value=value,
                confidence=confidence, source_session=source_session,
            ))
        session.commit()
        return True
    except Exception:
        session.rollback()
        return False
    finally:
        session.close()


def top_memories(user_id: int, limit: int = 8) -> list[dict]:
    """取该用户最相关的记忆（先按更新时间倒序，未来可换相关性排序）。"""
    session = SessionLocal()
    try:
        rows = session.execute(
            select(UserMemory)
            .where(UserMemory.user_id == user_id)
            .order_by(UserMemory.updated_at.desc())
            .limit(limit)
        ).scalars().all()
        return [
            {"kind": r.kind, "key": r.key, "value": r.value,
             "confidence": r.confidence, "updated_at": r.updated_at}
            for r in rows
        ]
    finally:
        session.close()


# ── 注入 ──

def format_memory_block(memories: list[dict]) -> str:
    """把记忆拼成一行行文本（给模型看的，不是给用户看的）。"""
    if not memories:
        return ""
    lines = ["[长期记忆] 以下是之前会话里确认过的事实，直接采信，不要重复追问："]
    for m in memories:
        lines.append(f"- {m['kind']}/{m['key']}：{m['value']}")
    return "\n".join(lines)


def build_injected(user_id: int, limit: int = 8) -> list[dict]:
    """构造注入块（Agent 的 ``injected`` 参数）。

    没有记忆时返回空列表 —— 不能塞一条空的 system 消息，那会干扰模型。
    """
    try:
        memories = top_memories(user_id, limit)
    except Exception:  # noqa: BLE001 - 记忆读失败不能影响对话
        return []
    block = format_memory_block(memories)
    if not block:
        return []
    return [{"role": "system", "content": block}]


# ── 提炼（规则版）──

def extract_from_messages(text: str) -> list[dict]:
    """从一段文本里抽事实。返回 [{kind,key,value}]，可空。

    规则版只做"能明确说出口的事实"，宁可少抽，不要乱抽。
    换成 LLM 提炼时，保持这个返回结构即可，上层不用改。
    """
    found: list[dict] = []
    for pattern, kind, key_fn in _RULES:
        m = pattern.search(text)
        if not m:
            continue
        try:
            key = key_fn(m)  # type: ignore[operator]
        except Exception:  # noqa: BLE001
            continue
        found.append({"kind": kind, "key": (key or kind)[:64], "value": m.group(0).strip()})
    return found


def process_outbox(limit: int = 20) -> dict:
    """消费待提炼队列：取 pending → 读该会话的 user 消息 → 规则提炼 → 写记忆。

    幂等：每条 outbox 处理一次后置 done；失败置 failed 并累加 attempts。
    """
    from garmin_coach.db.database import get_chat_history

    session = SessionLocal()
    try:
        rows = session.execute(
            select(MemoryOutbox)
            .where(MemoryOutbox.status == "pending")
            .order_by(MemoryOutbox.id)
            .limit(limit)
        ).scalars().all()
        jobs = [(r.id, r.session_id, r.user_id, r.attempts) for r in rows]
    finally:
        session.close()

    stats = {"scanned": len(jobs), "memories": 0, "failed": 0}
    for job_id, session_id, user_id, attempts in jobs:
        try:
            messages = get_chat_history(200, user_id, session_id=session_id)
            texts = [m["content"] for m in messages if m.get("role") == "user"]
            count = 0
            for text in texts:
                for item in extract_from_messages(text):
                    if upsert_memory(user_id, item["kind"], item["key"], item["value"],
                                     source_session=session_id):
                        count += 1
            stats["memories"] += count
            _set_outbox_status(job_id, "done")
        except Exception:  # noqa: BLE001
            stats["failed"] += 1
            _set_outbox_status(job_id, "failed", attempts + 1)
    return stats


def _set_outbox_status(job_id: int, status: str, attempts: int | None = None) -> None:
    session = SessionLocal()
    try:
        row = session.get(MemoryOutbox, job_id)
        if row:
            row.status = status
            row.updated_at = now_str()
            if attempts is not None:
                row.attempts = attempts
            session.commit()
    finally:
        session.close()


# ── 队列投递 ──

def enqueue_session(user_id: int, session_id: str) -> None:
    """会话结束时投一条待提炼任务（同一会话只投一次）。"""
    session = SessionLocal()
    try:
        exists = session.execute(
            select(MemoryOutbox.id).where(MemoryOutbox.session_id == session_id)
        ).first()
        if exists:
            return
        session.add(MemoryOutbox(session_id=session_id, user_id=user_id))
        session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()
