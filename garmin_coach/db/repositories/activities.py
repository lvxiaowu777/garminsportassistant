"""activities + strength_sets：活动读写、手动力量记录（幂等）。"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Optional

from sqlalchemy import delete, func, inspect, select, text, update

from garmin_coach.db.models import (
    Activity,
    AgentRun,
    AgentStep,
    ChatHistory,
    DailyPlan,
    DeviceToken,
    ExerciseName,
    HealthDaily,
    MemoryOutbox,
    StrengthSet,
    User,
    UserMemory,
    WatchBinding,
)

from .watches import get_active_brand

from ._base import (
    SessionLocal,
    _insert_stmt,
    _upsert,
    _upsert_append,
)


def _upsert_activity(session, date_str: str, act: dict, user_id: int = 0, brand: str = "garmin"):
    """插入或更新单条活动。"""
    cols = {
        "date": date_str,
        "user_id": user_id,
        "brand": brand,
        "activity_id": str(act.get("id", "")),
        "type": act.get("type"),
        "time": act.get("time"),
        "location": act.get("location"),
        "distance_km": act.get("distance_km"),
        "duration_min": act.get("duration_min"),
        "avg_pace": act.get("avg_pace"),
        "max_pace": act.get("max_pace"),
        "avg_hr": act.get("avg_hr"),
        "max_hr": act.get("max_hr"),
        "calories": act.get("calories"),
        "training_effect_aerobic": act.get("training_effect_aerobic"),
        "training_effect_anaerobic": act.get("training_effect_anaerobic"),
        "training_load": act.get("training_load"),
        "cadence_avg": act.get("cadence_avg"),
        "stride_length_cm": act.get("stride_length_cm"),
        "elevation_gain_m": act.get("elevation_gain_m"),
        "elevation_loss_m": act.get("elevation_loss_m"),
        "vo2max": act.get("vo2max"),
        "temperature_c": act.get("temperature_c"),
        "hr_zones": json.dumps(act.get("hr_zones"), ensure_ascii=False) if act.get("hr_zones") else None,
        "splits": json.dumps(act.get("splits"), ensure_ascii=False) if act.get("splits") else None,
    }
    cols = {k: v for k, v in cols.items() if v is not None}

    if not cols.get("activity_id"):
        return

    session.execute(_upsert(Activity, cols, ["user_id", "brand", "activity_id"]))


# ═══════════════════════════════════════════════
# 查询 API
# ═══════════════════════════════════════════════


def _get_activities(session, date_str: str, user_id: int, brand: str) -> list[dict]:
    rows = session.execute(
        select(Activity).where(
            Activity.date == date_str,
            Activity.user_id == user_id,
            Activity.brand == brand,
        ).order_by(Activity.time)
    ).scalars().all()
    result = []
    for r in rows:
        d = _activity_row(r)
        if d.get("type") == "strength_training":
            # 力量训练加动作摘要（如"二头弯举4组、深蹲3组"）
            sets = session.execute(
                select(StrengthSet).where(StrengthSet.activity_id == d["activity_id"]).order_by(StrengthSet.id)
            ).scalars().all()
            d["summary"] = "、".join(
                f"{s.name}{s.sets}组" if s.sets else s.name for s in sets
            )
        result.append(d)
    return result


def _activity_row(obj) -> dict:
    """把 ORM 对象转 dict，并解析 hr_zones/splits 的 JSON。"""
    d = {c.name: getattr(obj, c.name) for c in obj.__table__.columns}
    if d.get("hr_zones"):
        try:
            d["hr_zones"] = json.loads(d["hr_zones"])
        except (json.JSONDecodeError, TypeError):
            pass
    if d.get("splits"):
        try:
            d["splits"] = json.loads(d["splits"])
        except (json.JSONDecodeError, TypeError):
            pass
    return d


# ═══════════════════════════════════════════════
# 聊天历史
# ═══════════════════════════════════════════════


def get_activity(activity_id: str, user_id: int = 0, brand: str | None = None) -> Optional[dict]:
    """获取单次活动详情（含力量训练动作）。brand 缺省用当前启用品牌。"""
    brand = brand or get_active_brand(user_id)
    session = SessionLocal()
    try:
        row = session.execute(
            select(Activity).where(
                Activity.activity_id == str(activity_id),
                Activity.user_id == user_id,
                Activity.brand == brand,
            )
        ).scalars().first()
        if not row:
            return None
        result = _activity_row(row)
        sets = session.execute(
            select(StrengthSet).where(StrengthSet.activity_id == str(activity_id)).order_by(StrengthSet.id)
        ).scalars().all()
        result["strength_sets"] = [
            {"name": s.name, "sets": s.sets, "reps": s.reps, "reps_detail": s.reps_detail,
             "weight_kg": s.weight_kg, "equipment": s.equipment}
            for s in sets
        ]
        return result
    finally:
        session.close()


def delete_activity(activity_id: str, user_id: int) -> bool:
    """删除一条训练记录（activities + 对应 strength_sets）。"""
    session = SessionLocal()
    try:
        session.execute(
            StrengthSet.__table__.delete().where(StrengthSet.activity_id == str(activity_id))
        )
        result = session.execute(
            Activity.__table__.delete().where(
                Activity.activity_id == str(activity_id), Activity.user_id == user_id
            )
        )
        session.commit()
        return result.rowcount > 0
    finally:
        session.close()


def update_activity_exercises(activity_id: str, user_id: int, exercises: list) -> bool:
    """编辑力量训练动作明细：删除旧动作，插入新动作。"""
    session = SessionLocal()
    try:
        act = session.execute(
            select(Activity).where(
                Activity.activity_id == str(activity_id), Activity.user_id == user_id
            )
        ).scalars().first()
        if not act:
            return False
        session.execute(
            StrengthSet.__table__.delete().where(StrengthSet.activity_id == str(activity_id))
        )
        for ex in exercises or []:
            session.execute(_insert_stmt(StrengthSet, {
                "activity_id": str(activity_id),
                "name": ex.get("name", ""),
                "sets": ex.get("sets"),
                "reps": ex.get("reps"),
                "reps_detail": ex.get("reps_detail"),
                "weight_kg": ex.get("weight_kg"),
                "equipment": ex.get("equipment"),
            }))
        session.commit()
        return True
    except Exception:
        session.rollback()
        return False
    finally:
        session.close()


def _training_key(train_type: str, notes: str, exercises: list | None) -> str:
    """内容指纹：同一天、同类型、同内容的重复记录会落到同一个 activity_id 上。

    为什么不用时间戳：模型重试、用户重复说一遍、网络重发都会产生"看上去一样"
    的记录，时间戳每次不同 → 库里多一条。用内容指纹后，重复写入变成**更新**。
    """
    payload = json.dumps(
        {
            "t": train_type,
            "n": (notes or "").strip(),
            "e": sorted(
                (str(e.get("name", "")) for e in (exercises or [])),
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:10]


def record_training(user_id: int, train_type: str, duration_min: int, notes: str,
                    date_str: str = "", exercises: list | None = None) -> dict:
    """记录一次手动训练（幂等：重复调用不会产生重复行）。

    返回::

        {"ok": True, "created": bool, "activity_id": str,
         "added": [...], "merged": [...]}

    - **力量训练**：一天一条 activity；动作按 ``(activity_id, name)`` 合并，
      组数取大（先记 3 组、再说 4 组 → 记 4 组）；
    - **跑步/骑行等**：``activity_id = manual_{日期}_{内容指纹}``，
      同样内容重复记录就是 upsert 更新，不会多一条。
    """
    if train_type != "strength_training":
        # 跑步/骑行/游泳的唯一事实来源是手表同步，手动记录会造成两套数据打架
        return {
            "ok": False, "created": False, "activity_id": "", "added": [], "merged": [],
            "error": f"{train_type} 以手表数据为准，不接受手动记录",
        }

    date_str = date_str or dt.date.today().isoformat()
    time_str = dt.datetime.now().strftime("%H:%M")
    brand = get_active_brand(user_id)
    exercises = exercises or []
    key = _training_key(train_type, notes, exercises)

    session = SessionLocal()
    try:
        created = False
        merged: list[str] = []
        added: list[str] = []

        if train_type == "strength_training":
            # 力量训练一天只一条：先找当天的，没有才建
            existing = session.execute(
                select(Activity.activity_id).where(
                    Activity.user_id == user_id, Activity.date == date_str,
                    Activity.brand == brand, Activity.type == "strength_training",
                ).order_by(Activity.id).limit(1)
            ).first()
            if existing:
                activity_id = existing[0]
            else:
                activity_id = f"manual_{date_str}_{key}"
                session.execute(_insert_stmt(Activity, {
                    "date": date_str, "user_id": user_id, "brand": brand,
                    "activity_id": activity_id, "type": train_type,
                    "time": time_str, "duration_min": duration_min or 30,
                }))
                created = True
        else:
            activity_id = f"manual_{date_str}_{key}"
            session.execute(_upsert(
                Activity,
                {
                    "date": date_str, "user_id": user_id, "brand": brand,
                    "activity_id": activity_id, "type": train_type,
                    "time": time_str, "duration_min": duration_min or 30,
                },
                ["user_id", "brand", "activity_id"],
            ))

        note = f"[{time_str}] 手动记录: {train_type} {duration_min}min {notes}".strip()
        session.execute(
            _upsert_append(
                HealthDaily,
                {"date": date_str, "user_id": user_id, "brand": brand, "notes": note},
                ["date", "user_id", "brand"],
                "notes",
                note,
            )
        )

        # 动作明细：按 (activity_id, name) 合并，组数取大
        for ex in exercises:
            name = ex.get("name", "")
            if not name:
                continue
            row = session.execute(
                select(StrengthSet).where(
                    StrengthSet.activity_id == activity_id,
                    StrengthSet.name == name,
                ).order_by(StrengthSet.id).limit(1)
            ).scalars().first()

            if row is None:
                session.execute(_insert_stmt(StrengthSet, {
                    "activity_id": activity_id,
                    "name": name,
                    "sets": ex.get("sets"),
                    "reps": ex.get("reps"),
                    "reps_detail": ex.get("reps_detail"),
                    "weight_kg": ex.get("weight_kg"),
                    "equipment": ex.get("equipment"),
                }))
                added.append(name)
            else:
                new_sets = ex.get("sets")
                if new_sets and (row.sets or 0) < new_sets:
                    row.sets = new_sets
                for field in ("reps", "reps_detail", "weight_kg", "equipment"):
                    value = ex.get(field)
                    if value not in (None, ""):
                        setattr(row, field, value)
                merged.append(name)

        session.commit()
        return {
            "ok": True,
            "created": created,
            "activity_id": activity_id,
            "added": added,
            "merged": merged,
        }
    except Exception:
        session.rollback()
        return {"ok": False, "created": False, "activity_id": "", "added": [], "merged": []}
    finally:
        session.close()


# ═══════════════════════════════════════════════
# 用户系统
# ═══════════════════════════════════════════════
