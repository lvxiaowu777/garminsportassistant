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
    """保存训练计划（默认一周）。一个用户只保留一份活跃计划。"""
    normalized = _normalize_plan(plan_json)
    if normalized is None:
        return False

    days = normalized.get("days") or []
    dates = sorted(d.get("date") for d in days if isinstance(d, dict) and d.get("date"))
    if not dates:
        # 没有日期就没法判断有效期，直接拒绝（模型该补上日期）
        return False
    start_date, end_date = dates[0], dates[-1]

    if not date_str:
        date_str = dt.date.today().isoformat()

    plan_json = json.dumps(normalized, ensure_ascii=False)
    session = SessionLocal()
    try:
        # 一个用户只留一份活跃计划：存新的就把旧的整份替换掉
        session.execute(
            DailyPlan.__table__.delete().where(DailyPlan.user_id == user_id)
        )
        session.add(
            DailyPlan(
                user_id=user_id,
                date=date_str,
                start_date=start_date,
                end_date=end_date,
                plan_json=plan_json,
            )
        )
        session.commit()
        return True
    except Exception:
        session.rollback()
        return False
    finally:
        session.close()


def get_latest_plan(user_id: int) -> Optional[dict]:
    """取**当前有效**的计划（end_date >= 今天），没有返回 None。

    返回 ``{"days": [...], "start_date": "...", "end_date": "...", "days_left": N}``。
    过期计划不再返回——模型会知道"该重新排计划了"，而不是拿旧日期硬答。
    """
    today = dt.date.today().isoformat()
    session = SessionLocal()
    try:
        row = session.execute(
            select(DailyPlan)
            .where(
                DailyPlan.user_id == user_id,
                DailyPlan.end_date >= today,
            )
            .order_by(DailyPlan.id.desc())
            .limit(1)
        ).scalars().first()
        if not row or not row.plan_json:
            return None

        plan = _normalize_plan(row.plan_json) or {"days": []}
        plan["start_date"] = row.start_date or ""
        plan["end_date"] = row.end_date or ""
        try:
            end = dt.date.fromisoformat(row.end_date)
            plan["days_left"] = max(0, (end - dt.date.today()).days)
        except (TypeError, ValueError):
            plan["days_left"] = None
        return plan
    except Exception:
        return None
    finally:
        session.close()
