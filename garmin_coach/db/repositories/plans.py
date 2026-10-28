"""daily_plans：训练计划保存与读取。"""
from __future__ import annotations

import datetime as dt
import json
from typing import Optional

from sqlalchemy import delete, select

from garmin_coach.db.models import DailyPlan

from ._base import SessionLocal


def _normalize_plan(payload) -> dict | None:
    """把计划归一化成 {"days": [...]}；无法识别时返回 None。"""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return None
    if isinstance(payload, list):
        return {"days": payload}
    if isinstance(payload, dict):
        if isinstance(payload.get("days"), list):
            return payload
        # 允许 {"plan": [...]} 这类变体，宽松兜一下
        for key in ("plan", "days"):
            if isinstance(payload.get(key), list):
                return {**payload, "days": payload[key]}
    return None


def save_plan(user_id: int, plan_json: str, date_str: str = "") -> bool:
    """保存 AI 生成的 3 天计划。同一天覆盖（先删旧再插新）。"""
    normalized = _normalize_plan(plan_json)
    if normalized is None:
        return False
    plan_json = json.dumps(normalized, ensure_ascii=False)
    if not date_str:
        date_str = dt.date.today().isoformat()
    session = SessionLocal()
    try:
        session.execute(
            DailyPlan.__table__.delete().where(
                DailyPlan.user_id == user_id,
                DailyPlan.date == date_str,
            )
        )
        session.add(DailyPlan(user_id=user_id, date=date_str, plan_json=plan_json))
        session.commit()
        return True
    except Exception:
        session.rollback()
        return False
    finally:
        session.close()


def get_latest_plan(user_id: int) -> Optional[dict]:
    """取最近一次生成的计划，返回 {"days": [...]}（无则 None）。

    历史数据里可能有数组形状，这里读出来也归一化一次，保证调用方
    永远可以安全地写 plan.get("days")。异常一律返回 None，不影响主流程。
    """
    session = SessionLocal()
    try:
        row = session.execute(
            select(DailyPlan)
            .where(DailyPlan.user_id == user_id)
            .order_by(DailyPlan.id.desc())
            .limit(1)
        ).scalars().first()
        if not row or not row.plan_json:
            return None
        return _normalize_plan(row.plan_json)
    except Exception:
        return None
    finally:
        session.close()


if __name__ == "__main__":
    init_db()
    print(f"✅ 数据库初始化完成: {DATABASE_URL.split('@')[-1]}")
