"""待提炼队列：会话结束投一条，离线消费。

链路：
  会话回收 / 跨天 → enqueue_session()
  定时任务（或手动）→ process_outbox()
      → 读该会话的 user 消息
      → extract_with_llm()（失败退规则版）
      → upsert_memory()
      → consolidate()（管理 Agent：合并/删除）
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from garmin_coach.db.database import SessionLocal, get_chat_history
from garmin_coach.db.models import MemoryOutbox

from .extract import extract_with_llm, extract_with_rules
from .manage import consolidate
from .store import now_str, upsert_memory

log = logging.getLogger(__name__)


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


async def process_outbox(limit: int = 20) -> dict:
    """消费 pending 任务：提炼 → 写记忆 → 治理。"""
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

    stats = {"scanned": len(jobs), "memories": 0, "failed": 0, "consolidated": 0}
    for job_id, session_id, user_id, attempts in jobs:
        try:
            messages = get_chat_history(200, user_id, session_id=session_id)

            facts = await extract_with_llm(messages)
            if not facts:
                # LLM 不可用/抽不出 → 规则兜底（只扫 user 消息）
                text = "\n".join(
                    m["content"] for m in messages if m.get("role") == "user"
                )
                facts = extract_with_rules(text)

            count = 0
            for item in facts:
                if upsert_memory(
                    user_id,
                    item["kind"],
                    item["key"],
                    item["value"],
                    confidence=float(item.get("confidence") or 1.0),
                    source_session=session_id,
                ):
                    count += 1
            stats["memories"] += count

            consolidated = await consolidate(user_id)
            if consolidated.get("changed"):
                stats["consolidated"] += 1

            _set_status(job_id, "done")
        except Exception as exc:  # noqa: BLE001
            log.warning("记忆提炼失败 session=%s: %s", session_id, exc)
            stats["failed"] += 1
            _set_status(job_id, "failed", attempts + 1)
    return stats


def _set_status(job_id: int, status: str, attempts: int | None = None) -> None:
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
