"""
SQLAlchemy ORM 模型 —— 对应 MySQL 表结构。
从原 SQLite schema 1:1 复刻，字段类型映射到 MySQL。
"""
from datetime import datetime

from sqlalchemy import (
    Column,
    Float,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    phone = Column(String(32), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    created_at = Column(String(32), default=_now_str)
    # 需求8：用户档案（首次登录收集）
    height_cm = Column(Float)
    weight_kg = Column(Float)
    age = Column(Integer)
    gender = Column(String(8))
    body_fat_pct = Column(Float)
    muscle_mass_kg = Column(Float)
    goal = Column(String(32))          # half_marathon / full_marathon / shaping
    goal_target = Column(String(64))   # 目标成绩（如半马 2:00:00）
    training_years = Column(Float)     # 训练经验（年）
    training_freq_per_week = Column(Integer)  # 训练频率（每周几次）
    last_sync_date = Column(String(10))  # 上次同步日期 YYYY-MM-DD（判断当天是否首次同步）
    # 2026-08-20：训练类型 + 比赛列表（账号页面编辑 + AI 训练档案注入）
    training_types = Column(Text)  # JSON: ["marathon","half_marathon",...]
    races = Column(Text)           # JSON: [{"name":"上海马拉松","date":"2026-11-30"}]


class WatchBinding(Base):
    __tablename__ = "watch_bindings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False)
    brand = Column(String(16), nullable=False)
    account = Column(String(255), nullable=False)
    auth_token = Column(Text)
    token_expires = Column(String(64))
    enabled = Column(Integer, nullable=False, default=1)
    created_at = Column(String(32), default=_now_str)

    __table_args__ = (
        UniqueConstraint("user_id", "brand", name="uq_watch_binding_user_brand"),
    )


class HealthDaily(Base):
    __tablename__ = "health_daily"

    date = Column(String(10), nullable=False)
    user_id = Column(Integer, nullable=False, default=0)
    brand = Column(String(16), nullable=False, default="garmin")
    weekday = Column(String(8))
    sleep_hours = Column(Float)
    sleep_score = Column(Integer)
    sleep_deep_min = Column(Integer)
    sleep_rem_min = Column(Integer)
    sleep_light_min = Column(Integer)
    sleep_feedback = Column(String(64))
    resting_hr = Column(Integer)
    resting_hr_7d_avg = Column(Integer)
    hrv = Column(Integer)
    hrv_status = Column(String(64))
    hrv_baseline_low = Column(Integer)
    hrv_baseline_high = Column(Integer)
    body_battery_high = Column(Integer)
    body_battery_low = Column(Integer)
    body_battery_wake = Column(Integer)
    body_battery_charged = Column(Integer)
    body_battery_now = Column(Integer)
    stress_avg = Column(Integer)
    stress_qualifier = Column(String(64))
    readiness = Column(Integer)
    readiness_level = Column(String(64))
    steps = Column(Integer)
    kcal = Column(Integer)
    floors = Column(Integer)
    moderate_min = Column(Integer)
    vigorous_min = Column(Integer)
    training_status = Column(String(64))
    training_feedback = Column(String(64))
    acwr_ratio = Column(Float)
    acute_load = Column(Float)
    vo2max = Column(Float)
    fitness_age = Column(Float)
    race_5k = Column(String(32))
    race_10k = Column(String(32))
    race_half = Column(String(32))
    race_full = Column(String(32))
    weather_desc = Column(String(64))
    weather_high = Column(Integer)
    weather_low = Column(Integer)
    weather_humidity = Column(Integer)
    weather_aqi = Column(Integer)
    weather_aqi_category = Column(String(32))
    weather_uv = Column(Integer)
    weather_wind_kmh = Column(Float)
    weather_rain_prob = Column(Float)
    notes = Column(Text)
    location = Column(String(64))
    now_temp = Column(Integer)
    now_feels_like = Column(Integer)
    now_text = Column(String(64))
    now_precip = Column(Float)
    created_at = Column(String(32), default=_now_str)
    updated_at = Column(String(32), default=_now_str)

    __table_args__ = (
        PrimaryKeyConstraint("date", "user_id", "brand", name="pk_health_daily"),
    )


class Activity(Base):
    __tablename__ = "activities"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(String(10), nullable=False)
    user_id = Column(Integer, nullable=False, default=0)
    brand = Column(String(16), nullable=False, default="garmin")
    activity_id = Column(String(64), nullable=False)
    type = Column(String(32))
    time = Column(String(8))
    location = Column(String(128))
    distance_km = Column(Float)
    duration_min = Column(Float)
    avg_pace = Column(String(16))
    max_pace = Column(String(16))
    avg_hr = Column(Integer)
    max_hr = Column(Integer)
    calories = Column(Integer)
    training_effect_aerobic = Column(Float)
    training_effect_anaerobic = Column(Float)
    training_load = Column(Float)
    cadence_avg = Column(Float)
    stride_length_cm = Column(Float)
    elevation_gain_m = Column(Float)
    elevation_loss_m = Column(Float)
    vo2max = Column(Float)
    temperature_c = Column(Float)
    hr_zones = Column(Text)
    splits = Column(Text)
    created_at = Column(String(32), default=_now_str)

    __table_args__ = (
        UniqueConstraint("user_id", "brand", "activity_id", name="uq_activity_user_brand_aid"),
    )


class ChatHistory(Base):
    __tablename__ = "chat_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, default=0)
    role = Column(String(16), nullable=False)
    content = Column(Text, nullable=False)
    type = Column(String(16), default="text")       # text / voice
    audio_url = Column(String(255))                  # 语音消息的音频 URL
    duration_sec = Column(Integer)                   # 语音时长（秒）
    created_at = Column(String(32), default=_now_str)

    __table_args__ = (
        Index("idx_chat_user", "user_id"),
    )


class DeviceToken(Base):
    __tablename__ = "device_tokens"

    token = Column(String(255), primary_key=True)
    platform = Column(String(16), default="android")
    created_at = Column(String(32), default=_now_str)
    last_seen = Column(String(32), default=_now_str)


class ExerciseName(Base):
    """力量训练标准动作名，含别名（用于归一化）。"""

    __tablename__ = "exercise_names"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(64), unique=True, nullable=False)  # 标准名
    aliases = Column(Text)  # 别名，逗号分隔
    category = Column(String(32))  # 分类：手臂/肩/胸/背/腿/核心


class StrengthSet(Base):
    """力量训练动作详情，关联 activities.activity_id。"""

    __tablename__ = "strength_sets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    activity_id = Column(String(64), nullable=False, index=True)  # 关联 activities.activity_id
    name = Column(String(64), nullable=False)  # 动作名（标准名）
    sets = Column(Integer)     # 组数
    reps = Column(Integer)     # 每组次数（统一时）
    reps_detail = Column(String(128))  # 每组次数明细（如 15/10/10/10，与 reps 互斥）
    weight_kg = Column(Float)  # 重量（kg）
    equipment = Column(String(64))  # 器械（哑铃/杠铃/自重等）
    created_at = Column(String(32), default=_now_str)


class DailyPlan(Base):
    """AI 生成的未来 3 天训练计划（对话式 [PLAN] 标记落库）。"""

    __tablename__ = "daily_plans"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=False, default=0)
    date = Column(String(10), nullable=False)  # 生成日期 YYYY-MM-DD
    plan_json = Column(Text)  # 3 天计划 JSON 字符串
    created_at = Column(String(32), default=_now_str)

    __table_args__ = (
        Index("idx_plan_user", "user_id"),
    )
