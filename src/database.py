"""
数据库层 —— SQLAlchemy + MySQL 存储健康数据、活动记录、聊天历史。
替代原 SQLite 实现。对外函数签名保持不变，内部换成 SQLAlchemy Session。
"""
import sys
import os
import json
import datetime as dt
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from typing import Optional

# 加载 .env：优先项目目录（服务器 ~/garmin-coach/.env），fallback 本地 study/.env
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
if not ENV_PATH.exists():
    ENV_PATH = Path(r"C:\Users\boobyboy\study\.env")
if ENV_PATH.exists():
    with open(ENV_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())

from sqlalchemy import create_engine, select, func, inspect, text
from sqlalchemy.orm import sessionmaker
# 注意：这里不导入 sqlalchemy.dialects.mysql 的 insert —— 方言相关的语句统一由
# 下面的 _insert_stmt / _upsert 按 engine.dialect.name 分派，避免绑定到单一数据库。

from models import (
    Base, User, WatchBinding, HealthDaily, Activity, ChatHistory, DeviceToken,
    ExerciseName, StrengthSet, DailyPlan,
)

# 数据库连接串只从环境变量读。历史版本在这里硬编码了 MySQL 账号密码，
# 阶段 0 重构时移除 —— 连接串属于环境配置，不该写进代码，更不该进版本库。
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    raise RuntimeError(
        "未设置 DATABASE_URL 环境变量。\n"
        "  服务器（MySQL）: mysql+pymysql://<user>:<pass>@localhost:3306/garmin_coach?charset=utf8mb4\n"
        "  本地（SQLite）: sqlite:///./src/data/garmin_coach.db\n"
        "写进项目根目录的 .env 即可（该文件已 gitignore）。"
    )

engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=3600, echo=False)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


# ═══════════════════════════════════════════════
# 跨方言 SQL 辅助
# ═══════════════════════════════════════════════
#
# 历史实现直接用了 sqlalchemy.dialects.mysql 的 insert，把整个项目钉死在 MySQL 上：
# SQLite 下会抛 UnsupportedCompilationError（渲染不了 OnDuplicateClause），连启动都过不去。
# 下面按 engine.dialect.name 分派，让本地开发和集成测试可以用 SQLite 跑。
#
# 同理 func.concat 是 MySQL 专有函数，SQLite / PostgreSQL 要用 || 拼接，
# 所以「把一行追加到已有文本后面」也走辅助函数。


def _insert_stmt(model, values: dict):
    """按方言构造 INSERT（尚未附加冲突处理）。"""
    dialect = engine.dialect.name
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as _ins
    elif dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as _ins
    else:
        from sqlalchemy.dialects.mysql import insert as _ins
    return _ins(model).values(**values)


def _upsert(
    model,
    values: dict,
    conflict_cols: list[str],
    update_keys: list[str] | None = None,
    do_nothing: bool = False,
):
    """跨方言「插入或更新」。

    conflict_cols : 冲突判定键。MySQL 靠 PRIMARY/UNIQUE 自动识别，这里主要给 SQLite 的
                    ON CONFLICT 用。
    update_keys   : 冲突时要覆盖的列；None 表示「除冲突键外全部」。
    do_nothing    : True 表示冲突时保持原值（等价于 INSERT IGNORE）。
    """
    stmt = _insert_stmt(model, values)

    if do_nothing:
        if engine.dialect.name == "mysql":
            col = conflict_cols[0]
            return stmt.on_duplicate_key_update(**{col: getattr(model, col)})
        return stmt.on_conflict_do_nothing(index_elements=conflict_cols)

    keys = (
        update_keys
        if update_keys is not None
        else [k for k in values if k not in conflict_cols]
    )
    if not keys:
        return _upsert(model, values, conflict_cols, do_nothing=True)

    if engine.dialect.name == "mysql":
        return stmt.on_duplicate_key_update(**{k: stmt.inserted[k] for k in keys})
    return stmt.on_conflict_do_update(
        index_elements=conflict_cols,
        set_={k: stmt.excluded[k] for k in keys},
    )


def _append_concat(*parts):
    """字符串拼接的跨方言写法：MySQL 用 CONCAT()，其它方言用 ||。"""
    if engine.dialect.name == "mysql":
        return func.concat(*parts)
    expr = parts[0]
    for part in parts[1:]:
        expr = expr + part
    return expr


def _upsert_append(
    model,
    values: dict,
    conflict_cols: list[str],
    col_name: str,
    suffix: str,
):
    """upsert，冲突时把 suffix 追加到 col_name 的既有值后面（日志式字段用）。"""
    stmt = _insert_stmt(model, values)
    column = getattr(model, col_name)
    expr = _append_concat(func.coalesce(column, ""), "\n", suffix)
    if engine.dialect.name == "mysql":
        return stmt.on_duplicate_key_update(**{col_name: expr})
    return stmt.on_conflict_do_update(index_elements=conflict_cols, set_={col_name: expr})


def init_db():
    """建表（幂等）+ 预填标准动作名 + 补用户档案列 + 补聊天语音列。"""
    Base.metadata.create_all(engine)
    seed_exercise_names()
    _ensure_user_columns()
    _ensure_chat_columns()
    _ensure_strength_columns()
    _ensure_health_columns()


def _ensure_user_columns():
    """为已存在的 users 表补充需求8的档案列（幂等）。"""
    inspector = inspect(engine)
    existing = {c["name"] for c in inspector.get_columns("users")}
    new_cols = {
        "height_cm": "FLOAT", "weight_kg": "FLOAT", "age": "INT",
        "gender": "VARCHAR(8)", "body_fat_pct": "FLOAT", "muscle_mass_kg": "FLOAT",
        "goal": "VARCHAR(32)", "goal_target": "VARCHAR(64)",
        "training_years": "FLOAT", "training_freq_per_week": "INT",
        "last_sync_date": "VARCHAR(10)",
        # 2026-08-20 新增：训练类型 + 比赛列表（JSON TEXT）
        "training_types": "TEXT",
        "races": "TEXT",
    }
    with engine.begin() as conn:
        for col, typ in new_cols.items():
            if col not in existing:
                conn.execute(text(f"ALTER TABLE users ADD COLUMN {col} {typ}"))


def _ensure_chat_columns():
    """为已存在的 chat_history 表补充语音消息列（幂等）。"""
    inspector = inspect(engine)
    existing = {c["name"] for c in inspector.get_columns("chat_history")}
    new_cols = {
        "type": "VARCHAR(16) DEFAULT 'text'",
        "audio_url": "VARCHAR(255)",
        "duration_sec": "INT",
    }
    with engine.begin() as conn:
        for col, typ in new_cols.items():
            if col not in existing:
                conn.execute(text(f"ALTER TABLE chat_history ADD COLUMN {col} {typ}"))


def _ensure_strength_columns():
    """为已存在的 strength_sets 表补充器械/明细列（幂等）。"""
    inspector = inspect(engine)
    existing = {c["name"] for c in inspector.get_columns("strength_sets")}
    new_cols = {
        "reps_detail": "VARCHAR(128)",
        "equipment": "VARCHAR(64)",
    }
    with engine.begin() as conn:
        for col, typ in new_cols.items():
            if col not in existing:
                conn.execute(text(f"ALTER TABLE strength_sets ADD COLUMN {col} {typ}"))


def _ensure_health_columns():
    """为已存在的 health_daily 表补充后加列（幂等）。"""
    inspector = inspect(engine)
    existing = {c["name"] for c in inspector.get_columns("health_daily")}
    new_cols = {
        "body_battery_now": "INT",
    }
    with engine.begin() as conn:
        for col, typ in new_cols.items():
            if col not in existing:
                conn.execute(text(f"ALTER TABLE health_daily ADD COLUMN {col} {typ}"))


# ═══════════════════════════════════════════════
# 标准动作名（力量训练归一化）
# ═══════════════════════════════════════════════

DEFAULT_EXERCISES = [
    ("二头弯举", "举哑铃,哑铃弯举,杠铃弯举,弯举", "手臂"),
    ("三头下压", "三头,绳索下压,臂屈伸", "手臂"),
    ("肩推", "推肩,哑铃推举,杠铃推举,过头推举", "肩"),
    ("侧平举", "侧举,飞鸟", "肩"),
    ("前平举", "前举", "肩"),
    ("卧推", "平板卧推,杠铃卧推,哑铃卧推", "胸"),
    ("俯卧撑", "", "胸"),
    ("上斜卧推", "上斜推", "胸"),
    ("飞鸟", "哑铃飞鸟,夹胸", "胸"),
    ("引体向上", "引体,拉单杠", "背"),
    ("划船", "俯身划船,坐姿划船,杠铃划船", "背"),
    ("高位下拉", "下拉,宽握下拉", "背"),
    ("深蹲", "蹲,杠铃深蹲,哑铃深蹲,徒手深蹲", "腿"),
    ("硬拉", "罗马尼亚硬拉", "腿"),
    ("箭步蹲", "弓步蹲,弓箭步,箭步", "腿"),
    ("腿举", "倒蹬", "腿"),
    ("提踵", "踮脚", "腿"),
    ("卷腹", "仰卧起坐,腹肌,练腹", "核心"),
    ("平板支撑", "平板,核心支撑", "核心"),
    ("俄罗斯转体", "转体,俄式转体", "核心"),
]


def seed_exercise_names():
    """预填标准动作名（幂等，按 name 去重）。"""
    session = SessionLocal()
    try:
        for name, aliases, category in DEFAULT_EXERCISES:
            session.execute(
                _upsert(
                    ExerciseName,
                    {"name": name, "aliases": aliases, "category": category},
                    ["name"],
                    do_nothing=True,
                )
            )
        session.commit()
    finally:
        session.close()


def get_standard_exercise_names() -> list[str]:
    """返回所有标准动作名（供 AI 归一化参考）。"""
    session = SessionLocal()
    try:
        rows = session.execute(select(ExerciseName.name).order_by(ExerciseName.id)).all()
        return [r[0] for r in rows]
    finally:
        session.close()


def normalize_exercise_name(text: str) -> str:
    """规则归一化：把别名映射到标准名（查表兜底，主要归一化靠 AI）。"""
    session = SessionLocal()
    try:
        rows = session.execute(select(ExerciseName.name, ExerciseName.aliases)).all()
        for name, aliases in rows:
            for alias in (aliases or "").split(","):
                alias = alias.strip()
                if alias and alias in text:
                    return name
        return text
    finally:
        session.close()


def _row_dict(row) -> Optional[dict]:
    if row is None:
        return None
    return dict(row._mapping)


# ═══════════════════════════════════════════════
# 健康数据 CRUD
# ═══════════════════════════════════════════════

def upsert_health_daily(data: dict, user_id: int = 0, brand: str = "garmin"):
    """插入或更新一天的完整健康数据。data 格式对齐 garmin_sync / coros_sync 的输出。"""
    sleep = data.get("sleep") or {}
    recovery = data.get("recovery") or {}
    daily = data.get("daily") or {}
    fitness = data.get("fitness") or {}
    weather = data.get("weather") or {}
    race = fitness.get("race_predictions") or {}
    age = fitness.get("age") or {}

    cols = {
        "date": data["date"],
        "user_id": user_id,
        "brand": brand,
        "weekday": data.get("weekday"),
        "sleep_hours": sleep.get("hours"),
        "sleep_score": sleep.get("score"),
        "sleep_deep_min": sleep.get("deep_min"),
        "sleep_rem_min": sleep.get("rem_min"),
        "sleep_light_min": sleep.get("light_min"),
        "sleep_feedback": sleep.get("feedback"),
        "resting_hr": recovery.get("resting_hr"),
        "resting_hr_7d_avg": recovery.get("resting_hr_7d_avg"),
        "hrv": recovery.get("hrv"),
        "hrv_status": recovery.get("hrv_status"),
        "hrv_baseline_low": recovery.get("hrv_baseline_low"),
        "hrv_baseline_high": recovery.get("hrv_baseline_high"),
        "body_battery_high": recovery.get("body_battery_high"),
        "body_battery_low": recovery.get("body_battery_low"),
        "body_battery_wake": recovery.get("body_battery_wake"),
        "body_battery_charged": recovery.get("body_battery_charged"),
        "body_battery_now": recovery.get("body_battery_now"),
        "stress_avg": recovery.get("stress_avg"),
        "stress_qualifier": recovery.get("stress_qualifier"),
        "readiness": recovery.get("readiness"),
        "readiness_level": recovery.get("readiness_level"),
        "steps": daily.get("steps"),
        "kcal": daily.get("kcal"),
        "floors": daily.get("floors"),
        "moderate_min": daily.get("moderate_min"),
        "vigorous_min": daily.get("vigorous_min"),
        "training_status": fitness.get("training_status"),
        "training_feedback": fitness.get("training_feedback"),
        "acwr_ratio": fitness.get("acwr_ratio"),
        "acute_load": fitness.get("acute_load"),
        "vo2max": fitness.get("vo2max"),
        "fitness_age": age.get("fitness"),
        "race_5k": race.get("5k"),
        "race_10k": race.get("10k"),
        "race_half": race.get("half"),
        "race_full": race.get("full"),
        "weather_desc": weather.get("desc"),
        "weather_high": weather.get("high"),
        "weather_low": weather.get("low"),
        "weather_humidity": weather.get("humidity"),
        "weather_aqi": weather.get("aqi"),
        "weather_aqi_category": weather.get("aqi_category"),
        "weather_uv": weather.get("uv_index"),
        "weather_wind_kmh": weather.get("wind_kmh"),
        "weather_rain_prob": weather.get("rain_prob"),
        "location": weather.get("location"),
        "now_temp": weather.get("now_temp"),
        "now_feels_like": weather.get("now_feels_like"),
        "now_text": weather.get("now_text"),
        "now_precip": weather.get("now_precip"),
        "updated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    # 清理 None 值让 MySQL 存 NULL
    cols = {k: v for k, v in cols.items() if v is not None}

    session = SessionLocal()
    try:
        session.execute(_upsert(HealthDaily, cols, ["date", "user_id", "brand"]))

        # 同步活动
        for act in data.get("activities") or []:
            _upsert_activity(session, data["date"], act, user_id, brand)

        session.commit()
    finally:
        session.close()


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

def get_active_brand(user_id: int = 0) -> str:
    """当前启用的手表品牌（仪表盘数据源）。返回第一个 enabled=1 的绑定，无则默认 garmin。"""
    session = SessionLocal()
    try:
        row = session.execute(
            select(WatchBinding.brand)
            .where(WatchBinding.user_id == user_id, WatchBinding.enabled == 1)
            .order_by(WatchBinding.id)
            .limit(1)
        ).first()
        return row[0] if row else "garmin"
    finally:
        session.close()


def has_health_data(user_id: int = 0) -> bool:
    """该用户是否有任何健康数据（用于判断是否首次同步）。"""
    session = SessionLocal()
    try:
        n = session.execute(
            select(func.count()).select_from(HealthDaily).where(HealthDaily.user_id == user_id)
        ).scalar() or 0
        return n > 0
    finally:
        session.close()


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


def get_day(date_str: str, user_id: int = 0, brand: str | None = None) -> Optional[dict]:
    """获取某天完整数据（含活动）。brand 缺省用当前启用品牌。"""
    brand = brand or get_active_brand(user_id)
    session = SessionLocal()
    try:
        row = session.execute(
            select(HealthDaily).where(
                HealthDaily.date == date_str,
                HealthDaily.user_id == user_id,
                HealthDaily.brand == brand,
            )
        ).scalars().first()
        if not row:
            return None
        result = _activity_row(row)
        result["activities"] = _get_activities(session, date_str, user_id, brand)
        return result
    finally:
        session.close()


def get_recent(days: int = 7, user_id: int = 0, brand: str | None = None) -> list[dict]:
    """获取最近 N 天数据（含活动）。brand 缺省用当前启用品牌。"""
    brand = brand or get_active_brand(user_id)
    session = SessionLocal()
    try:
        end = dt.date.today()
        start = end - dt.timedelta(days=days - 1)
        rows = session.execute(
            select(HealthDaily).where(
                HealthDaily.date >= start.isoformat(),
                HealthDaily.date <= end.isoformat(),
                HealthDaily.user_id == user_id,
                HealthDaily.brand == brand,
            ).order_by(HealthDaily.date.desc())
        ).scalars().all()
        results = []
        for row in rows:
            d = _activity_row(row)
            d["activities"] = _get_activities(session, d["date"], user_id, brand)
            results.append(d)
        return results
    finally:
        session.close()


def get_dashboard(user_id: int = 0) -> dict:
    """仪表盘聚合数据：今日状态 + 7天趋势 + 本周摘要。"""
    today = dt.date.today().isoformat()
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()

    today_data = get_day(today, user_id) or {}
    yesterday_data = get_day(yesterday, user_id) or {}

    # 只有睡眠是"昨晚"的（滞后一天），其余字段都是今天的实时数据。
    for k in ("sleep_hours", "sleep_score", "sleep_deep_min", "sleep_rem_min",
              "sleep_light_min", "sleep_feedback"):
        if today_data.get(k) is None and yesterday_data.get(k) is not None:
            today_data[k] = yesterday_data[k]

    recent = get_recent(7, user_id)

    total_dist = 0.0
    total_dur = 0.0
    run_count = 0
    hrv_vals = []
    rhr_vals = []
    sleep_vals = []
    daily_stats = []

    for d in recent:
        for a in d.get("activities") or []:
            if a.get("distance_km"):
                total_dist += a["distance_km"]
            if a.get("duration_min"):
                total_dur += a["duration_min"]
            if a.get("type") == "running":
                run_count += 1
        if d.get("hrv"):
            hrv_vals.append({"date": d["date"], "value": d["hrv"]})
        if d.get("resting_hr"):
            rhr_vals.append({"date": d["date"], "value": d["resting_hr"]})
        if d.get("sleep_hours"):
            sleep_vals.append({"date": d["date"], "value": d["sleep_hours"]})

        daily_stats.append({
            "date": d["date"],
            "weekday": d.get("weekday", ""),
            "hrv": d.get("hrv"),
            "resting_hr": d.get("resting_hr"),
            "sleep_hours": d.get("sleep_hours"),
            "sleep_score": d.get("sleep_score"),
            "body_battery_high": d.get("body_battery_high"),
            "steps": d.get("steps"),
            "activities": [{
                "type": a.get("type"),
                "distance_km": a.get("distance_km"),
                "duration_min": a.get("duration_min"),
                "avg_hr": a.get("avg_hr"),
                "avg_pace": a.get("avg_pace"),
            } for a in d.get("activities") or []],
        })

    return {
        "today": today_data,
        "yesterday": yesterday_data,
        "recent_7d": {
            "total_distance_km": round(total_dist, 1),
            "total_duration_min": round(total_dur, 2),
            "run_count": run_count,
            "avg_hrv": round(sum(v["value"] for v in hrv_vals) / len(hrv_vals)) if hrv_vals else None,
            "avg_resting_hr": round(sum(v["value"] for v in rhr_vals) / len(rhr_vals)) if rhr_vals else None,
            "avg_sleep_h": round(sum(v["value"] for v in sleep_vals) / len(sleep_vals), 1) if sleep_vals else None,
        },
        "charts": {
            "hrv_trend": hrv_vals,
            "rhr_trend": rhr_vals,
            "sleep_trend": sleep_vals,
        },
        "daily_stats": daily_stats,
    }


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


def get_weather(date_str: str, user_id: int = 0, brand: str | None = None) -> Optional[dict]:
    brand = brand or get_active_brand(user_id)
    session = SessionLocal()
    try:
        row = session.execute(
            select(HealthDaily).where(
                HealthDaily.date == date_str,
                HealthDaily.user_id == user_id,
                HealthDaily.brand == brand,
            )
        ).scalars().first()
        if not row:
            return None
        return {
            "date": row.date,
            "weather_desc": row.weather_desc,
            "weather_high": row.weather_high,
            "weather_low": row.weather_low,
            "weather_humidity": row.weather_humidity,
            "weather_aqi": row.weather_aqi,
            "weather_aqi_category": row.weather_aqi_category,
            "weather_uv": row.weather_uv,
            "weather_wind_kmh": row.weather_wind_kmh,
            "weather_rain_prob": row.weather_rain_prob,
        }
    finally:
        session.close()


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

def add_chat(role: str, content: str, user_id: int = 0, msg_type: str = "text",
             audio_url: str | None = None, duration_sec: int | None = None):
    session = SessionLocal()
    try:
        session.add(ChatHistory(role=role, content=content, user_id=user_id,
                                type=msg_type, audio_url=audio_url, duration_sec=duration_sec))
        session.commit()
    finally:
        session.close()


def get_chat_history(limit: int = 50, user_id: int = 0, date_str: str = "") -> list[dict]:
    session = SessionLocal()
    try:
        if not date_str:
            date_str = dt.date.today().isoformat()  # 默认今天
        q = select(ChatHistory).where(
            ChatHistory.user_id == user_id,
            ChatHistory.created_at.like(f"{date_str}%"),
        )
        rows = session.execute(
            q.order_by(ChatHistory.id.desc()).limit(limit)
        ).scalars().all()
        return [{c.name: getattr(r, c.name) for c in ChatHistory.__table__.columns} for r in reversed(rows)]
    finally:
        session.close()


def get_chat_dates(user_id: int = 0, days: int = 7) -> list[str]:
    """列出近 days 天有聊天记录的日期（供前端日期下拉框）。"""
    session = SessionLocal()
    try:
        cutoff = (dt.datetime.now() - dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        rows = session.execute(
            select(func.substr(ChatHistory.created_at, 1, 10))
            .where(ChatHistory.user_id == user_id, ChatHistory.created_at >= cutoff)
            .distinct()
            .order_by(func.substr(ChatHistory.created_at, 1, 10).desc())
        ).all()
        return [r[0] for r in rows]
    finally:
        session.close()


def cleanup_old_chat(days: int = 7):
    """删除 days 天前的聊天记录，含语音音频文件。"""
    session = SessionLocal()
    try:
        cutoff = (dt.datetime.now() - dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        # 1. 查旧语音消息的音频文件名
        rows = session.execute(
            select(ChatHistory.audio_url).where(
                ChatHistory.created_at < cutoff,
                ChatHistory.type == "voice",
                ChatHistory.audio_url.isnot(None),
            )
        ).all()
        # 2. 删除音频文件
        audio_dir = Path(__file__).resolve().parent.parent / "static" / "audio"
        for r in rows:
            url = r[0] or ""
            filename = url.rsplit("/", 1)[-1] if url else ""
            if filename:
                try:
                    (audio_dir / filename).unlink(missing_ok=True)
                except Exception:
                    pass
        # 3. 删除聊天记录
        session.execute(
            ChatHistory.__table__.delete().where(ChatHistory.created_at < cutoff)
        )
        session.commit()
    finally:
        session.close()


def clear_chat_history(user_id: int = 0):
    session = SessionLocal()
    try:
        session.execute(
            ChatHistory.__table__.delete().where(ChatHistory.user_id == user_id)
        )
        session.commit()
    finally:
        session.close()


# ═══════════════════════════════════════════════
# 笔记
# ═══════════════════════════════════════════════

def add_note(date_str: str, text: str):
    """追加笔记到某天（历史遗留：固定 user_id=0 / brand=garmin）。"""
    now = dt.datetime.now().strftime("%H:%M")
    entry = f"[{now}] {text}"
    session = SessionLocal()
    try:
        row = session.execute(
            select(HealthDaily.notes).where(
                HealthDaily.date == date_str,
                HealthDaily.user_id == 0,
                HealthDaily.brand == "garmin",
            )
        ).scalars().first()
        new_notes = (row + "\n" + entry) if row else entry
        session.execute(
            _upsert_append(
                HealthDaily,
                {"date": date_str, "user_id": 0, "brand": "garmin", "notes": new_notes},
                ["date", "user_id", "brand"],
                "notes",
                entry,
            )
        )
        session.commit()
        return entry
    finally:
        session.close()


# ═══════════════════════════════════════════════
# 手动训练记录
# ═══════════════════════════════════════════════

def record_training(user_id: int, train_type: str, duration_min: int, notes: str,
                    date_str: str = "", exercises: list | None = None) -> bool:
    """记录一次手动训练（写入 activities + health_daily.notes + strength_sets）。

    力量训练一天只记一次：复用当天已有的 strength_training，追加动作明细。
    """
    date_str = date_str or dt.date.today().isoformat()
    time_str = dt.datetime.now().strftime("%H:%M")
    brand = get_active_brand(user_id)
    session = SessionLocal()
    try:
        activity_id = f"manual_{int(dt.datetime.now().timestamp())}"
        # 力量训练一天只记一次：复用当天已有的 strength_training
        if train_type == "strength_training":
            existing = session.execute(
                select(Activity.activity_id).where(
                    Activity.user_id == user_id, Activity.date == date_str,
                    Activity.brand == brand, Activity.type == "strength_training",
                ).order_by(Activity.id).limit(1)
            ).first()
            if existing:
                activity_id = existing[0]
            else:
                session.execute(_insert_stmt(Activity, {
                    "date": date_str, "user_id": user_id, "brand": brand,
                    "activity_id": activity_id, "type": train_type,
                    "time": time_str, "duration_min": duration_min or 30,
                }))
        else:
            session.execute(_insert_stmt(Activity, {
                "date": date_str, "user_id": user_id, "brand": brand,
                "activity_id": activity_id, "type": train_type,
                "time": time_str, "duration_min": duration_min or 30,
            }))

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
        # 力量训练结构化动作详情
        for ex in exercises or []:
            session.execute(_insert_stmt(StrengthSet, {
                "activity_id": activity_id,
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


# ═══════════════════════════════════════════════
# 用户系统
# ═══════════════════════════════════════════════

def create_user(phone: str, password_hash: str) -> Optional[int]:
    """创建用户，成功返回 user_id，手机号重复返回 None。"""
    session = SessionLocal()
    try:
        exists = session.execute(
            select(User.id).where(User.phone == phone)
        ).first()
        if exists:
            return None
        user = User(phone=phone, password_hash=password_hash)
        session.add(user)
        session.commit()
        session.refresh(user)
        return user.id
    except Exception:
        session.rollback()
        return None
    finally:
        session.close()


def get_user_by_phone(phone: str) -> Optional[dict]:
    session = SessionLocal()
    try:
        row = session.execute(select(User).where(User.phone == phone)).scalars().first()
        return _orm_dict(row) if row else None
    finally:
        session.close()


def get_user_by_id(user_id: int) -> Optional[dict]:
    session = SessionLocal()
    try:
        row = session.execute(select(User).where(User.id == user_id)).scalars().first()
        return _orm_dict(row) if row else None
    finally:
        session.close()


_PROFILE_COLS = ("height_cm", "weight_kg", "age", "gender", "body_fat_pct",
                 "muscle_mass_kg", "goal", "goal_target", "training_years",
                 "training_freq_per_week", "training_types", "races")


def get_user_profile(user_id: int) -> dict:
    """获取用户档案（需求8字段 + 训练类型/比赛）。"""
    session = SessionLocal()
    try:
        row = session.execute(select(User).where(User.id == user_id)).scalars().first()
        if not row:
            return {}
        data = {c: getattr(row, c) for c in _PROFILE_COLS}
        # JSON 字段解码（数据库存的是 JSON 字符串，应用层用 list/dict）
        for key in ("training_types", "races"):
            raw = data.get(key)
            if raw:
                try:
                    data[key] = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    data[key] = []
            else:
                data[key] = []
        return data
    finally:
        session.close()


def update_user_profile(user_id: int, data: dict) -> bool:
    """更新用户档案（只接受允许的字段，忽略 None）。"""
    values = {}
    for k, v in data.items():
        if k not in _PROFILE_COLS or v is None:
            continue
        # JSON 字段需要序列化
        if k in ("training_types", "races"):
            if isinstance(v, (list, dict)):
                values[k] = json.dumps(v, ensure_ascii=False)
            else:
                values[k] = v
        else:
            values[k] = v
    if not values:
        return False
    session = SessionLocal()
    try:
        session.execute(User.__table__.update().where(User.id == user_id).values(**values))
        session.commit()
        return True
    finally:
        session.close()


def get_last_sync_date(user_id: int) -> str:
    """返回用户上次同步日期（YYYY-MM-DD），无则空串。"""
    session = SessionLocal()
    try:
        row = session.execute(select(User.last_sync_date).where(User.id == user_id)).first()
        return row[0] if row and row[0] else ""
    finally:
        session.close()


def update_last_sync_date(user_id: int, date_str: str):
    """更新用户上次同步日期。"""
    session = SessionLocal()
    try:
        session.execute(User.__table__.update().where(User.id == user_id).values(last_sync_date=date_str))
        session.commit()
    finally:
        session.close()


def _orm_dict(obj) -> dict:
    return {c.name: getattr(obj, c.name) for c in obj.__table__.columns}


# ── 手表绑定 ──

def bind_watch(user_id: int, brand: str, account: str, auth_token: str = "", token_expires: str = "") -> bool:
    """绑定手表品牌。已存在则更新 token。"""
    session = SessionLocal()
    try:
        existing = session.execute(
            select(WatchBinding).where(
                WatchBinding.user_id == user_id, WatchBinding.brand == brand
            )
        ).scalars().first()
        if existing:
            existing.account = account
            existing.auth_token = auth_token
            existing.token_expires = token_expires
        else:
            session.add(WatchBinding(
                user_id=user_id, brand=brand, account=account,
                auth_token=auth_token, token_expires=token_expires,
            ))
        session.commit()
        return True
    except Exception:
        session.rollback()
        return False
    finally:
        session.close()


def unbind_watch(user_id: int, brand: str) -> bool:
    session = SessionLocal()
    try:
        result = session.execute(
            WatchBinding.__table__.delete().where(
                WatchBinding.user_id == user_id, WatchBinding.brand == brand
            )
        )
        session.commit()
        return result.rowcount > 0
    finally:
        session.close()


def get_watch_bindings(user_id: int) -> list[dict]:
    session = SessionLocal()
    try:
        rows = session.execute(
            select(WatchBinding.id, WatchBinding.brand, WatchBinding.account,
                   WatchBinding.enabled, WatchBinding.created_at)
            .where(WatchBinding.user_id == user_id)
        ).all()
        return [dict(r._mapping) for r in rows]
    finally:
        session.close()


def set_watch_enabled(user_id: int, brand: str, enabled: bool) -> bool:
    session = SessionLocal()
    try:
        result = session.execute(
            WatchBinding.__table__.update()
            .where(WatchBinding.user_id == user_id, WatchBinding.brand == brand)
            .values(enabled=1 if enabled else 0)
        )
        session.commit()
        return result.rowcount > 0
    finally:
        session.close()


def select_watch(user_id: int, brand: str) -> bool:
    """单选设备：把 brand 设为 enabled=1，其他品牌设为 enabled=0。"""
    session = SessionLocal()
    try:
        exists = session.execute(
            select(WatchBinding.id).where(
                WatchBinding.user_id == user_id, WatchBinding.brand == brand
            )
        ).first()
        if not exists:
            return False
        session.execute(
            WatchBinding.__table__.update().where(WatchBinding.user_id == user_id).values(enabled=0)
        )
        session.execute(
            WatchBinding.__table__.update().where(
                WatchBinding.user_id == user_id, WatchBinding.brand == brand
            ).values(enabled=1)
        )
        session.commit()
        return True
    finally:
        session.close()


# ── 手表同步辅助（供 garmin_server_sync / coros_server_sync 使用） ──

def get_watch_enabled(user_id: int, brand: str) -> bool:
    """查某用户某品牌是否启用。未绑定时返回 True（视为不拦截）。"""
    session = SessionLocal()
    try:
        row = session.execute(
            select(WatchBinding.enabled).where(
                WatchBinding.user_id == user_id, WatchBinding.brand == brand
            )
        ).first()
        if row is None:
            return True
        return row[0] == 1
    finally:
        session.close()


def get_enabled_user_ids(brand: str) -> list[int]:
    """查所有启用了某品牌的 user_id（定时同步用）。"""
    session = SessionLocal()
    try:
        rows = session.execute(
            select(WatchBinding.user_id).where(
                WatchBinding.brand == brand, WatchBinding.enabled == 1
            ).distinct()
        ).all()
        return [r[0] for r in rows]
    finally:
        session.close()


# ═══════════════════════════════════════════════
# 设备推送 Token
# ═══════════════════════════════════════════════

def register_device_token(token: str, platform: str = "android") -> bool:
    session = SessionLocal()
    try:
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        session.execute(
            _upsert(
                DeviceToken,
                {"token": token, "platform": platform, "last_seen": now},
                ["token"],
                update_keys=["platform", "last_seen"],
            )
        )
        session.commit()
        return True
    except Exception:
        session.rollback()
        return False
    finally:
        session.close()


def get_active_tokens() -> list[str]:
    session = SessionLocal()
    try:
        cutoff = (dt.datetime.now() - dt.timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        rows = session.execute(
            select(DeviceToken.token).where(DeviceToken.last_seen > cutoff)
        ).all()
        return [r[0] for r in rows]
    finally:
        session.close()


def remove_token(token: str):
    session = SessionLocal()
    try:
        session.execute(DeviceToken.__table__.delete().where(DeviceToken.token == token))
        session.commit()
    finally:
        session.close()


# ═══════════════════════════════════════════════
# 推送提醒查询（供 push_service.check_and_notify 使用，保持原全局语义）
# ═══════════════════════════════════════════════

def get_global_day_health(date_str: str) -> Optional[dict]:
    """查某天健康数据（不过滤用户，取第一条，历史遗留语义）。"""
    session = SessionLocal()
    try:
        row = session.execute(
            select(HealthDaily.hrv, HealthDaily.sleep_hours, HealthDaily.resting_hr)
            .where(HealthDaily.date == date_str)
            .limit(1)
        ).first()
        return dict(row._mapping) if row else None
    finally:
        session.close()


def get_global_activity_count(date_str: str) -> int:
    """查某天活动总数（不过滤用户，历史遗留语义）。"""
    session = SessionLocal()
    try:
        return session.execute(
            select(func.count()).select_from(Activity).where(Activity.date == date_str)
        ).scalar() or 0
    finally:
        session.close()


# ═══════════════════════════════════════════════
# AI 每日训练计划（[PLAN] 标记落库）
# ═══════════════════════════════════════════════

def save_plan(user_id: int, plan_json: str, date_str: str = "") -> bool:
    """保存 AI 生成的 3 天计划。同一天覆盖（先删旧再插新）。"""
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
    """取最近一次生成的计划，返回 plan dict（无则 None）。任何异常都返回 None，绝不影响主流程。"""
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
        try:
            return json.loads(row.plan_json)
        except (json.JSONDecodeError, TypeError):
            return None
    except Exception:
        return None
    finally:
        session.close()


if __name__ == "__main__":
    init_db()
    print(f"✅ 数据库初始化完成: {DATABASE_URL.split('@')[-1]}")
