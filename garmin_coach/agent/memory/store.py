"""长期记忆的存储层：`user_memory` 表的读写。

和"聊天记录"的区别：
- 聊天记录是原话，7 天后删；
- 这里是结论（kind/key/value），长期保留，同 key 覆盖。
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import delete, select

from garmin_coach.db.database import SessionLocal
from garmin_coach.db.models import UserMemory

KINDS = ("injury", "goal", "preference", "constraint", "plan_decision")


def now_str() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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


def delete_memory(user_id: int, kind: str, key: str) -> bool:
    """删除一条记忆（用户说"这个已经过去了"时用）。删掉了返回 True。"""
    session = SessionLocal()
    try:
        result = session.execute(
            delete(UserMemory).where(
                UserMemory.user_id == user_id,
                UserMemory.kind == kind,
                UserMemory.key == key,
            )
        )
        session.commit()
        return bool(result.rowcount)
    except Exception:
        session.rollback()
        return False
    finally:
        session.close()


def top_memories(user_id: int, limit: int = 8) -> list[dict]:
    """取该用户最相关的记忆（当前按更新时间倒序）。"""
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


def all_memories(user_id: int, limit: int = 100) -> list[dict]:
    """管理 Agent 用：拿全量记忆做合并/删除。"""
    return top_memories(user_id, limit)


def replace_memories(user_id: int, items: list[dict]) -> int:
    """用一批记忆整体替换该用户的记忆（管理 Agent 的输出落地）。

    返回写入条数；失败返回 -1（保留原数据）。
    """
    session = SessionLocal()
    try:
        session.execute(delete(UserMemory).where(UserMemory.user_id == user_id))
        count = 0
        for item in items:
            kind = item.get("kind")
            key = (item.get("key") or "").strip()[:64]
            value = (item.get("value") or "").strip()
            if kind not in KINDS or not key or not value:
                continue
            session.add(UserMemory(
                user_id=user_id, kind=kind, key=key, value=value,
                confidence=float(item.get("confidence") or 1.0),
                source_session=item.get("source_session"),
            ))
            count += 1
        session.commit()
        return count
    except Exception:
        session.rollback()
        return -1
    finally:
        session.close()
