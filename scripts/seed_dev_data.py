"""生成本地可重复的开发数据（MySQL）。

用途：跑分 / 演示 / 联调都需要一份**可重建**的数据；现在真实数据不可控，
所以用脚本灌固定种子（`--seed 42`），每次结果一致。

用法（在项目根目录）：

    python scripts/seed_dev_data.py --password 你的密码
    python scripts/seed_dev_data.py --password xxx --users 20 --days 14 --reset

连接参数优先级：命令行 > 环境变量(MYSQL_*) > 项目 .env > 默认值。
注意：密码不要写进代码，用 --password 或环境变量。

产出：
    - 建库 `garmin_coach`（不存在时）
    - N 个用户（id 从 1001 开始），每人 days 天健康数据 + 跑步/力量活动
    - 用户 1001 额外带 6 条聊天记录 + 一份 3 天计划
    - 打印示例 token（可直接拿去调接口）
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))          # garmin_coach 包在项目根
sys.path.insert(0, str(ROOT / "src"))  # auth 等旧模块在 src

# 先加载 .env，后面从 DATABASE_URL 推导 MySQL 连接信息
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False)
except ImportError:
    pass


def _db_defaults() -> dict:
    """从 DATABASE_URL 解析 host/port/user/password/db（解析不了就回退）。"""
    import re
    import urllib.parse as up

    url = os.environ.get("DATABASE_URL", "")
    m = re.match(r"mysql\+pymysql://([^@]+)@([^/]+)/([^?]+)", url)
    if not m:
        return {}
    userinfo, hostport, db = m.groups()
    user, _, password = userinfo.partition(":")
    host, _, port = hostport.partition(":")
    return {
        "host": host or "127.0.0.1",
        "port": int(port or 3306),
        "user": up.unquote(user),
        "password": up.unquote(password),
        "db": up.unquote(db),
    }

WEEKDAY = ["一", "二", "三", "四", "五", "六", "日"]

PACE = ["5'10\"", "5'25\"", "5'38\"", "5'52\"", "6'05\"", "6'20\""]


def parse_args() -> argparse.Namespace:
    d = _db_defaults()
    p = argparse.ArgumentParser(description="灌本地开发数据到 MySQL")
    p.add_argument("--host", default=os.environ.get("MYSQL_HOST", d.get("host", "127.0.0.1")))
    p.add_argument("--port", type=int, default=int(os.environ.get("MYSQL_PORT", d.get("port", 3306))))
    p.add_argument("--user", default=os.environ.get("MYSQL_USER", d.get("user", "root")))
    p.add_argument("--password", default=os.environ.get("MYSQL_PASSWORD", d.get("password", "")))
    p.add_argument("--db", default=os.environ.get("MYSQL_DB", d.get("db", "garmin_coach")))
    p.add_argument("--users", type=int, default=20, help="造多少个用户")
    p.add_argument("--days", type=int, default=14, help="每人多少天健康数据")
    p.add_argument("--seed", type=int, default=42, help="随机种子（保证可复现）")
    p.add_argument("--reset", action="store_true", help="先删掉这批用户的数据再灌")
    return p.parse_args()


def ensure_database(a: argparse.Namespace) -> None:
    """建库（不存在时）。表由 database.init_db() 建。"""
    import pymysql

    # 库已经能连上就不用 CREATE（普通用户没有 CREATE DATABASE 权限）
    try:
        conn = pymysql.connect(
            host=a.host, port=a.port, user=a.user, password=a.password,
            database=a.db, charset="utf8mb4",
        )
        conn.close()
        return
    except pymysql.err.OperationalError as exc:
        if exc.args and exc.args[0] != 1049:   # 1049 = Unknown database
            raise

    conn = pymysql.connect(
        host=a.host, port=a.port, user=a.user, password=a.password, charset="utf8mb4"
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{a.db}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        conn.commit()
    finally:
        conn.close()


def day_payload(date: dt.date, rng: random.Random, i: int) -> dict:
    """一天的健康数据（结构对齐 fetch_day 的嵌套输出）。"""
    hrv_base = 62 - i * 0.6 + rng.uniform(-4, 4)
    hrv = round(hrv_base)
    sleep_h = round(rng.uniform(6.2, 8.2), 2)
    rhr = round(46 + rng.uniform(-3, 4))
    bb_high = rng.randint(72, 96)
    bb_low = rng.randint(18, 40)
    bb_wake = rng.randint(55, 85)
    bb_now = rng.randint(30, 78)
    steps = rng.randint(4200, 14000)

    return {
        "date": date.isoformat(),
        "weekday": WEEKDAY[date.weekday()],
        "sleep": {
            "hours": sleep_h,
            "score": rng.randint(63, 92),
            "deep_min": rng.randint(55, 110),
            "rem_min": rng.randint(60, 105),
            "light_min": rng.randint(180, 260),
            "feedback": rng.choice(["POSITIVE_LONG_AND_REFRESHING", "POSITIVE_REFRESHING"]),
        },
        "recovery": {
            "resting_hr": rhr,
            "resting_hr_7d_avg": rhr + rng.randint(-1, 2),
            "hrv": hrv,
            "hrv_status": "BALANCED" if hrv > 55 else "UNBALANCED",
            "hrv_baseline_low": 50,
            "hrv_baseline_high": 74,
            "body_battery_high": bb_high,
            "body_battery_low": bb_low,
            "body_battery_wake": bb_wake,
            "body_battery_now": bb_now,
            "body_battery_charged": rng.randint(45, 70),
            "stress_avg": rng.randint(22, 45),
            "stress_qualifier": "BALANCED",
            "readiness": rng.randint(60, 95),
            "readiness_level": "HIGH",
        },
        "daily": {
            "steps": steps,
            "kcal": rng.randint(1800, 2600),
            "floors": rng.randint(3, 18),
            "moderate_min": rng.randint(10, 50),
            "vigorous_min": rng.randint(0, 25),
        },
        "fitness": {
            "vo2max": round(50 + rng.uniform(0, 4), 1),
            "training_status": "有效",
            "training_feedback": "PRODUCTIVE",
            "acwr_ratio": round(rng.uniform(0.85, 1.35), 2),
            "acute_load": round(rng.uniform(280, 460), 1),
            "race_predictions": {
                "5k": "23:10", "10k": "48:30", "half": "1:46:20", "full": "3:52:00",
            },
            "age": {"fitness": 26.4},
        },
        "weather": {
            "desc": rng.choice(["晴", "多云", "小雨"]),
            "high": rng.randint(18, 30),
            "low": rng.randint(10, 20),
            "humidity": rng.randint(40, 85),
            "aqi": rng.randint(30, 110),
            "aqi_category": "良",
            "uv_index": rng.randint(2, 9),
            "wind_kmh": rng.randint(3, 18),
            "rain_prob": round(rng.uniform(0, 0.6), 2),
            "location": "西安",
            "now_temp": rng.randint(15, 28),
            "now_feels_like": rng.randint(15, 30),
            "now_text": "晴",
            "now_precip": 0.0,
        },
    }


def make_run(date: dt.date, rng: random.Random, seq: int) -> dict:
    dist = round(rng.choice([5.0, 6.2, 8.0, 10.0, 12.5]), 1)
    dur = round(dist * rng.uniform(5.6, 6.6), 1)
    pace = PACE[rng.randrange(len(PACE))]
    avg_hr = rng.randint(138, 165)
    return {
        "id": f"dev{seq}",
        "type": "running",
        "time": f"{rng.randint(6, 20):02d}:{rng.choice([0, 10, 20, 30, 40]):02d}",
        "location": rng.choice(["曲江池", "田径场", "环城公园"]),
        "distance_km": dist,
        "duration_min": dur,
        "avg_pace": pace,
        "max_pace": "4'58\"",
        "avg_hr": avg_hr,
        "max_hr": avg_hr + rng.randint(8, 25),
        "calories": int(dist * 62),
        "training_effect_aerobic": round(rng.uniform(2.8, 4.6), 1),
        "training_effect_anaerobic": round(rng.uniform(0.0, 1.6), 1),
        "cadence_avg": round(rng.uniform(168, 184), 1),
        "elevation_gain_m": rng.randint(5, 60),
        "hr_zones": {
            "z1_sec": rng.randint(60, 300), "z2_sec": rng.randint(900, 2400),
            "z3_sec": rng.randint(300, 1200), "z4_sec": rng.randint(60, 600),
            "z5_sec": rng.randint(0, 180),
        },
        "splits": [
            {"km": k + 1, "pace": pace, "hr": avg_hr + rng.randint(-6, 6)}
            for k in range(int(dist))
        ],
    }


def make_strength(date: dt.date, rng: random.Random, seq: int) -> dict:
    return {
        "id": f"dev{seq}",
        "type": "strength_training",
        "time": f"{rng.randint(18, 21):02d}:30",
        "location": "家",
        "duration_min": rng.choice([35, 40, 45]),
        "avg_hr": rng.randint(95, 125),
        "calories": rng.randint(180, 320),
    }


def seed(a: argparse.Namespace) -> None:
    rng = random.Random(a.seed)

    os.environ["DATABASE_URL"] = (
        f"mysql+pymysql://{a.user}:{a.password}@{a.host}:{a.port}/{a.db}?charset=utf8mb4"
    )

    from garmin_coach.db import database  # noqa: E402  必须在设置 DATABASE_URL 之后导入
    from garmin_coach.auth import create_token, hash_password
    from garmin_coach.db.models import (  # noqa: E402
        Activity, ChatHistory, DailyPlan, HealthDaily, StrengthSet, User, WatchBinding,
    )
    from sqlalchemy import delete, select  # noqa: E402

    database.init_db()

    uids = [1001 + i for i in range(a.users)]
    session = database.SessionLocal()
    try:
        if a.reset:
            session.execute(delete(HealthDaily).where(HealthDaily.user_id.in_(uids)))
            session.execute(delete(Activity).where(Activity.user_id.in_(uids)))
            session.execute(delete(ChatHistory).where(ChatHistory.user_id.in_(uids)))
            session.execute(delete(DailyPlan).where(DailyPlan.user_id.in_(uids)))
            session.execute(delete(WatchBinding).where(WatchBinding.user_id.in_(uids)))
            session.execute(delete(User).where(User.id.in_(uids)))
            session.commit()
            print(f"已清空 {len(uids)} 个用户的旧数据")

        pw = hash_password("123456")
        today = dt.date.today()
        existing = {r[0] for r in session.execute(select(User.id).where(User.id.in_(uids)))}

        for idx, uid in enumerate(uids):
            if uid not in existing:
                session.add(User(
                    id=uid,
                    phone=f"199{10000000 + idx}",
                    password_hash=pw,
                    height_cm=175.0, weight_kg=66.0, age=24, gender="male",
                    goal="half_marathon", goal_target="1:45:00",
                    training_years=2.0, training_freq_per_week=4,
                    training_types='["half_marathon"]',
                    races='[{"name":"2026 西安半马","date":"2026-12-06"}]',
                ))
                session.add(WatchBinding(
                    user_id=uid, brand="garmin",
                    account=f"dev{idx}@example.com", enabled=1,
                ))
        session.commit()
        print(f"用户/绑定：{len(uids)} 个（已存在则跳过）")

        # 14 天健康数据 + 活动
        health_rows = 0
        act_rows = 0
        run_days = {0, 3, 6, 9}       # 距今天数（含今天，方便演示"今天已运动"）
        strength_days = {2, 7}
        seq = 0
        for uid in uids:
            for offset in range(a.days):
                date = today - dt.timedelta(days=offset)
                data = day_payload(date, rng, offset)

                acts = []
                seq += 1
                if offset in run_days:
                    acts.append(make_run(date, rng, seq))
                if offset in strength_days:
                    acts.append(make_strength(date, rng, seq))
                if acts:
                    data["activities"] = acts
                database.upsert_health_daily(data, user_id=uid, brand="garmin")
                health_rows += 1
                act_rows += len(acts)

                # 力量训练的动作明细
                for act in acts:
                    if act["type"] == "strength_training":
                        for name, sets, reps, weight in (
                            ("深蹲", 4, 10, 40.0),
                            ("卧推", 3, 8, 35.0),
                            ("平板支撑", 3, None, None),
                        ):
                            session.add(StrengthSet(
                                activity_id=act["id"], name=name, sets=sets,
                                reps=reps, weight_kg=weight, equipment="杠铃",
                            ))
            session.commit()
        print(f"健康数据：{health_rows} 行；活动：{act_rows} 条")

        # 用户 1001 的聊天记录 + 计划（方便测对话/前端）
        session.execute(delete(ChatHistory).where(ChatHistory.user_id == 1001))
        session.execute(delete(DailyPlan).where(DailyPlan.user_id == 1001))
        for role, content in (
            ("user", "今晚跑不跑？"),
            ("assistant", "今天安全档位 EASY，HRV 62（正常），建议轻松跑 5 公里、心率 140 以下。"),
            ("user", "昨天睡得怎么样"),
            ("assistant", "昨天睡了 7.6 小时，深睡 95 分钟，得分 84。"),
        ):
            session.add(ChatHistory(
                user_id=1001, role=role, content=content,
                session_id=f"u1001-{today.isoformat()}",
            ))
        session.add(DailyPlan(
            user_id=1001,
            date=today.isoformat(),
            plan_json=(
                '{"days": [{"date": "%s", "day": "今天", "run": "easy 5km 心率<140",'
                ' "strength": "", "note": "注意补水"}]}' % today.isoformat()
            ),
        ))
        session.commit()
    finally:
        session.close()

    token = create_token(1001)
    print("\n完成。示例 token（user 1001）：")
    print(token)
    print("\n把它写进 .env：")
    print(
        f"DATABASE_URL=mysql+pymysql://{a.user}:{a.password}@{a.host}:{a.port}/{a.db}?charset=utf8mb4"
    )


def main() -> None:
    a = parse_args()
    if not a.password:
        print("缺少 MySQL 密码：用 --password 或设置环境变量 MYSQL_PASSWORD")
        sys.exit(2)
    ensure_database(a)
    seed(a)


if __name__ == "__main__":
    main()
