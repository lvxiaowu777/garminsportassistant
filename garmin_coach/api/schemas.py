"""API 请求 / 响应模型。

只放 pydantic 模型，不放任何业务逻辑——路由里 `req.model_dump()` 之后就该交给 service。
"""
from __future__ import annotations

from pydantic import BaseModel

# ── 鉴权 ──


class RegisterRequest(BaseModel):
    phone: str
    password: str


class LoginRequest(BaseModel):
    phone: str
    password: str


# ── 手表 ──


class WatchBindRequest(BaseModel):
    brand: str
    account: str
    password: str = ""
    # 区服：当前没有品牌用到，保留字段是为了兼容客户端仍会传它
    region: str = "cn"


class WatchMfaRequest(BaseModel):
    mfa_code: str


class WatchToggleRequest(BaseModel):
    brand: str
    enabled: bool


class WatchSelectRequest(BaseModel):
    brand: str


# ── 健康数据 / 记录 ──


class UpdateExercisesRequest(BaseModel):
    exercises: list = []


class ExerciseItem(BaseModel):
    name: str                          # 动作名（卧推/深蹲…）
    sets: int | None = None            # 组数
    reps: int | None = None            # 每组次数
    reps_detail: str | None = None     # 次数明细（如 12,10,8）
    weight_kg: float | None = None     # 重量
    equipment: str | None = None       # 器械


class RecordTrainingRequest(BaseModel):
    type: str = "strength_training"  # running / strength_training / cycling 等
    notes: str = ""                   # 训练内容描述
    duration_min: int = 0             # 时长（分钟）
    date: str = ""                    # 日期，默认今天
    exercises: list[ExerciseItem] = []  # 动作明细（计时器结束后补录，可空）


# ── 用户档案 ──


class UpdateProfileRequest(BaseModel):
    height_cm: float | None = None
    weight_kg: float | None = None
    age: int | None = None
    gender: str | None = None
    body_fat_pct: float | None = None
    muscle_mass_kg: float | None = None
    goal: str | None = None
    goal_target: str | None = None
    training_years: float | None = None
    training_freq_per_week: int | None = None
    # 2026-08-20 新增：训练类型 + 比赛列表
    training_types: list[str] | None = None
    races: list[dict] | None = None


# ── 对话 ──


class ChatRequest(BaseModel):
    message: str


# ── 推送 ──


class PushRegisterRequest(BaseModel):
    token: str
    platform: str = "android"


class PushTestRequest(BaseModel):
    title: str = "Garmin Coach"
    body: str = "测试推送 🏃"
    click_action: str = "dashboard"
