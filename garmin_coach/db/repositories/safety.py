"""safety_snapshots 表：每日安全档位快照（回测用）。"""
from __future__ import annotations

import json
from typing import Optional

from garmin_coach.db.models import SafetySnapshot

from ._base import SessionLocal, _upsert


def save_safety_snapshot(
    *,
    date_str: str,
    user_id: int,
    max_tier: str,
    blockers: list[str],
    hard_days: int,
    hrv_baseline: float | None,
    hrmax: int | None,
) -> bool:
    """记一条当天的档位快照；同一天只保留第一次（冲突时不动）。"""
    session = SessionLocal()
    try:
        session.execute(
            _upsert(
                SafetySnapshot,
                {
                    "date": date_str,
                    "user_id": user_id,
                    "max_tier": max_tier,
                    "blockers": json.dumps(blockers, ensure_ascii=False),
                    "hard_days": hard_days,
                    "hrv_baseline": hrv_baseline,
                    "hrmax": hrmax,
                },
                ["date", "user_id"],
                do_nothing=True,
            )
        )
        session.commit()
        return True
    except Exception:
        session.rollback()
        return False
    finally:
        session.close()


def get_safety_snapshot(date_str: str, user_id: int) -> Optional[dict]:
    """取某天的档位快照（没有返回 None）。"""
    session = SessionLocal()
    try:
        row = session.query(SafetySnapshot).filter(
            SafetySnapshot.date == date_str,
            SafetySnapshot.user_id == user_id,
        ).first()
        if row is None:
            return None
        return {
            "date": row.date,
            "user_id": row.user_id,
            "max_tier": row.max_tier,
            "blockers": json.loads(row.blockers) if row.blockers else [],
            "hard_days": row.hard_days,
            "hrv_baseline": row.hrv_baseline,
            "hrmax": row.hrmax,
        }
    finally:
        session.close()
