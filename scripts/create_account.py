"""建一个自己的账号，并从演示账号复制数据。

用法：
    python scripts/create_account.py --phone adimn --password 'xxx'
    python scripts/create_account.py --phone adimn --password 'xxx' --copy-from 1001

复制内容：档案、手表绑定、健康日数据、活动（activity_id 会重写避免跨用户串）、
力量动作、训练计划、长期记忆。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from garmin_coach.auth import hash_password  # noqa: E402
from garmin_coach.db.database import SessionLocal, create_user, get_user_by_phone  # noqa: E402
from garmin_coach.db.models import (  # noqa: E402
    Activity,
    DailyPlan,
    HealthDaily,
    StrengthSet,
    User,
    UserMemory,
    WatchBinding,
)

PROFILE_FIELDS = [
    "height_cm", "weight_kg", "age", "gender", "body_fat_pct", "muscle_mass_kg",
    "goal", "goal_target", "training_years", "training_freq_per_week",
    "training_types", "races",
]


def copy_user_data(src_id: int, dst_id: int) -> dict:
    session = SessionLocal()
    counts: dict[str, int] = {}
    try:
        src = session.get(User, src_id)
        dst = session.get(User, dst_id)
        if not src or not dst:
            raise SystemExit("源或目标用户不存在")
        for field in PROFILE_FIELDS:
            setattr(dst, field, getattr(src, field))

        # 手表绑定
        binds = session.query(WatchBinding).filter(WatchBinding.user_id == src_id).all()
        for b in binds:
            session.add(WatchBinding(
                user_id=dst_id, brand=b.brand, account=b.account,
                auth_token=b.auth_token, token_expires=b.token_expires,
                enabled=b.enabled,
            ))
        counts["watch_bindings"] = len(binds)

        # 健康日数据
        rows = session.query(HealthDaily).filter(HealthDaily.user_id == src_id).all()
        for r in rows:
            data = {c.name: getattr(r, c.name) for c in HealthDaily.__table__.columns}
            data["user_id"] = dst_id
            session.add(HealthDaily(**data))
        counts["health_daily"] = len(rows)

        # 活动 + 力量动作（activity_id 重写）
        acts = session.query(Activity).filter(Activity.user_id == src_id).all()
        for a in acts:
            new_aid = f"{a.activity_id}_u{dst_id}"
            data = {c.name: getattr(a, c.name) for c in Activity.__table__.columns}
            data.pop("id", None)          # 自增主键，不能复制
            data["user_id"] = dst_id
            data["activity_id"] = new_aid
            session.add(Activity(**data))
            for s in session.query(StrengthSet).filter(StrengthSet.activity_id == a.activity_id).all():
                session.add(StrengthSet(
                    activity_id=new_aid, name=s.name, sets=s.sets, reps=s.reps,
                    reps_detail=s.reps_detail, weight_kg=s.weight_kg, equipment=s.equipment,
                ))
        counts["activities"] = len(acts)

        # 计划（重写日期字段保留）
        plans = session.query(DailyPlan).filter(DailyPlan.user_id == src_id).all()
        for p in plans:
            session.add(DailyPlan(
                user_id=dst_id, date=p.date, start_date=p.start_date,
                end_date=p.end_date, plan_json=p.plan_json,
            ))
        counts["daily_plans"] = len(plans)

        # 长期记忆
        mems = session.query(UserMemory).filter(UserMemory.user_id == src_id).all()
        for m in mems:
            session.add(UserMemory(
                user_id=dst_id, kind=m.kind, key=m.key, value=m.value,
                confidence=m.confidence, source_session=m.source_session,
            ))
        counts["user_memory"] = len(mems)

        session.commit()
        return counts
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phone", required=True, help="登录名（可直接用 adimn）")
    parser.add_argument("--password", required=True)
    parser.add_argument("--copy-from", type=int, default=0, help="从哪个 user_id 复制数据")
    args = parser.parse_args()

    phone = args.phone.strip()
    existing = get_user_by_phone(phone)
    if existing:
        uid = existing["id"]
        print(f"账号已存在，直接用它：{phone}（user_id={uid}）")
    else:
        uid = create_user(phone, hash_password(args.password))
        if uid is None:
            raise SystemExit("创建失败")
        print(f"✅ 账号已创建：{phone}（user_id={uid}）")
    if args.copy_from:
        counts = copy_user_data(args.copy_from, uid)
        print("已复制：", counts)
    print("登录：手机号填", phone)


if __name__ == "__main__":
    main()
