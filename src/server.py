"""
Garmin Coach 独立版 —— FastAPI 后端
提供 REST API + WebSocket 聊天 + 静态文件服务
"""
import sys
import os
import json
import re
import logging
import threading
import datetime as dt
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 把 src 加入 path
SRC_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC_DIR))

# 项目根也要加：新架构包 garmin_coach/ 在根目录下。
# 用 `python src/server.py` 启动时 sys.path[0] 是 src/，不包含项目根，
# 不加这一行 `import garmin_coach` 会 ModuleNotFoundError。
PROJECT_ROOT = SRC_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from database import (
    init_db, get_day, get_recent, get_dashboard, get_activity,
    get_weather, add_chat, get_chat_history, clear_chat_history,
    add_note, upsert_health_daily,
    create_user, get_user_by_phone, get_user_by_id,
    bind_watch, unbind_watch, get_watch_bindings, set_watch_enabled, select_watch,
    record_training, get_standard_exercise_names,
    get_chat_dates, cleanup_old_chat,
    get_user_profile, update_user_profile,
    get_last_sync_date, update_last_sync_date, delete_activity, update_activity_exercises,
    save_plan, get_latest_plan,
)
from llm_client import chat_stream, get_provider, PROVIDERS, asr_transcribe
from safety_rules import evaluate, tier_description
from push_service import init_firebase, register_device, send_push, get_active_tokens, check_and_notify
from auth import hash_password, verify_password, create_token, get_current_user
from garmin_server_sync import login_and_store, complete_mfa, sync_user_garmin, sync_all_users
from coros_server_sync import login_and_store as coros_login, sync_user_coros, sync_all_coros_users

# ── FastAPI 应用 ──
app = FastAPI(title="Garmin Coach", version="1.0.0")

# 让 Agent 的工具调用过程能在服务器日志里看到（agent / garmin.server 两个 logger）。
# 只在没人配过 handler 时设一次，避免覆盖 uvicorn 自己的配置。
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

log = logging.getLogger("garmin.server")

# 静态文件
STATIC_DIR = SRC_DIR.parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
AUDIO_DIR = STATIC_DIR / "audio"
AUDIO_DIR.mkdir(exist_ok=True)

# 健康数据目录（旧的 markdown 数据源；不硬编码用户名）
HEALTH_DIR = (
    Path(os.environ.get("GARMIN_STUDY_DIR") or (Path.home() / "study"))
    / "知识库"
    / "健康"
)


# ═══════════════════════════════════════════════
# 安全档位计算（内联 deployment）
# ═══════════════════════════════════════════════

def compute_safety(user_id: int = 0) -> dict:
    """用最近数据计算安全档位。"""
    today = dt.date.today().isoformat()
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()

    today_data = get_day(today, user_id) or {}
    yesterday_data = get_day(yesterday, user_id) or {}
    base = today_data if today_data else yesterday_data

    if not base:
        return {"max_tier": "LONG", "tier_description": tier_description("LONG"),
                "blockers": [], "notes": ["⚠️ 无数据，无法评估，按正常状态处理"]}

    # 最近 7 天 HRV
    recent = get_recent(7, user_id)
    hrv_vals = [d.get("hrv") for d in recent if d.get("hrv")]
    recent_hrv_avg = sum(hrv_vals) / len(hrv_vals) if hrv_vals else None

    # 最近 3 天高强度
    hard_days = 0
    for d in recent[:3]:
        for a in d.get("activities") or []:
            hr = a.get("avg_hr") or 0
            dur = a.get("duration_min") or 0
            if (hr >= 155 and dur >= 30) or dur >= 75:
                hard_days += 1
                break

    # 昨天活动
    y_acts = []
    for a in (yesterday_data.get("activities") or []):
        y_acts.append({
            "type": a.get("type"),
            "dist_km": a.get("distance_km"),
            "dur_min": a.get("duration_min"),
            "avg_hr": a.get("avg_hr"),
        })

    weather = today_data.get("weather") or {}
    flat = {
        "date": base.get("date"),
        "hrv": base.get("hrv"),
        "hrv_status": base.get("hrv_status"),
        "sleep_hours": base.get("sleep_hours"),
        "sleep_score": base.get("sleep_score"),
        "resting_hr": base.get("resting_hr"),
        "body_battery_high": base.get("body_battery_high"),
        "training_readiness": base.get("readiness"),
        "training_readiness_level": base.get("readiness_level"),
        "acwr_ratio": base.get("acwr_ratio"),
        "training_feedback": base.get("training_feedback"),
        "training_status": base.get("training_status"),
        "weather": {
            "high": weather.get("weather_high"),
            "humidity": weather.get("weather_humidity"),
            "aqi": weather.get("weather_aqi"),
            "aqi_category": weather.get("weather_aqi_category"),
            "uv_index": weather.get("weather_uv"),
            "rain_prob": weather.get("weather_rain_prob"),
        } if weather else {},
        "yesterday_activities": y_acts,
    }

    max_tier, blockers, notes = evaluate(flat, recent_hrv_avg=recent_hrv_avg, recent_hard_days=hard_days)

    return {
        "max_tier": max_tier,
        "tier_description": tier_description(max_tier),
        "blockers": blockers,
        "notes": notes,
        "recent_hrv_avg": round(recent_hrv_avg) if recent_hrv_avg else None,
        "recent_hard_days": hard_days,
    }


# ═══════════════════════════════════════════════
# 构建 LLM 上下文
# ═══════════════════════════════════════════════

def build_chat_context(user_id: int = 0) -> dict:
    """构建完整的聊天上下文数据。"""
    now = dt.datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    hour = now.hour

    if hour < 5:
        period = "深夜"
    elif hour < 10:
        period = "早晨"
    elif hour < 14:
        period = "上午"
    elif hour < 18:
        period = "下午"
    elif hour < 22:
        period = "傍晚"
    else:
        period = "夜晚"

    dashboard = get_dashboard(user_id)
    safety = compute_safety(user_id)

    # 2026-08-20：用户训练档案注入（驱动 AI 训练阶段判断 + 比赛倒计时）
    profile = get_user_profile(user_id)
    upcoming = []
    for r in (profile.get("races") or []):
        try:
            d = dt.date.fromisoformat(r["date"])
        except (KeyError, ValueError, TypeError):
            continue
        days = (d - dt.date.today()).days
        if days >= 0:
            upcoming.append({"name": r.get("name", ""), "date": r["date"], "days_until": days})
    upcoming.sort(key=lambda x: x["days_until"])
    profile_payload = {
        "goal": profile.get("goal"),
        "goal_target": profile.get("goal_target"),
        "training_types": profile.get("training_types") or [],
        "upcoming_races": upcoming,
        "soonest_race_days": upcoming[0]["days_until"] if upcoming else None,
    }

    return {
        "datetime": now.strftime("%Y-%m-%d %H:%M"),
        "period": period,
        "today": dashboard.get("today") or {},
        "recent_7d": dashboard.get("recent_7d") or {},
        "daily_stats": dashboard.get("daily_stats") or [],
        "safety": safety,
        "standard_exercises": get_standard_exercise_names(),
        "plan": get_latest_plan(user_id),
        "profile": profile_payload,
    }


# ═══════════════════════════════════════════════
# API 路由
# ═══════════════════════════════════════════════

# ── 鉴权 ──

class RegisterRequest(BaseModel):
    phone: str
    password: str


class LoginRequest(BaseModel):
    phone: str
    password: str


class WatchBindRequest(BaseModel):
    brand: str
    account: str
    password: str = ""
    region: str = "cn"  # Coros 区服：cn/eu/us/asia


@app.post("/api/auth/register")
async def api_register(req: RegisterRequest):
    """手机号注册。"""
    phone = req.phone.strip()
    if not phone or len(phone) < 11:
        raise HTTPException(400, "请输入正确的手机号")
    if len(req.password) < 6:
        raise HTTPException(400, "密码至少 6 位")

    existing = get_user_by_phone(phone)
    if existing:
        raise HTTPException(409, "该手机号已注册，请直接登录")

    pw_hash = hash_password(req.password)
    user_id = create_user(phone, pw_hash)
    if user_id is None:
        raise HTTPException(500, "注册失败，请重试")

    token = create_token(user_id)
    return {"ok": True, "user_id": user_id, "token": token}


@app.post("/api/auth/login")
async def api_login(req: LoginRequest):
    """手机号登录。"""
    phone = req.phone.strip()
    user = get_user_by_phone(phone)
    if not user:
        raise HTTPException(401, "该手机号未注册")

    if not verify_password(req.password, user["password_hash"]):
        raise HTTPException(401, "密码错误")

    token = create_token(user["id"])
    return {"ok": True, "user_id": user["id"], "token": token}


@app.get("/api/auth/me")
async def api_me(user_id: int = Depends(get_current_user)):
    """返回当前登录用户的基础信息 + 是否完成过 onboarding。
    2026-08-20：去掉了 phone/created_at/nickname 字段（账号页面不再展示），
    新增 profile_completed 标志（用 goal IS NOT NULL 判断）+ training_types + races。
    """
    if user_id == 0:
        raise HTTPException(401, "请先登录")
    profile = get_user_profile(user_id)
    if not profile:
        raise HTTPException(404, "用户不存在")
    return {
        "user_id": user_id,
        "profile_completed": profile.get("goal") is not None,
        "height_cm": profile.get("height_cm"),
        "weight_kg": profile.get("weight_kg"),
        "age": profile.get("age"),
        "gender": profile.get("gender"),
        "body_fat_pct": profile.get("body_fat_pct"),
        "muscle_mass_kg": profile.get("muscle_mass_kg"),
        "goal": profile.get("goal"),
        "goal_target": profile.get("goal_target"),
        "training_years": profile.get("training_years"),
        "training_freq_per_week": profile.get("training_freq_per_week"),
        "training_types": profile.get("training_types") or [],
        "races": profile.get("races") or [],
    }


# ── 手表绑定 ──

class WatchMfaRequest(BaseModel):
    mfa_code: str


@app.post("/api/watch/bind")
async def api_bind_watch(req: WatchBindRequest, user_id: int = Depends(get_current_user)):
    """绑定手表品牌（需登录）。Garmin 走官方登录（动态判断 MFA）。"""
    if user_id == 0:
        raise HTTPException(401, "请先登录")
    if req.brand not in ("garmin", "coros", "apple", "huawei", "xiaomi"):
        raise HTTPException(400, f"不支持的手表品牌: {req.brand}")

    # Garmin：用账号密码登录官方，验证凭据 + 动态判断是否需要两步验证
    if req.brand == "garmin":
        if not req.account or not req.password:
            raise HTTPException(400, "Garmin 需要邮箱和密码")
        needs_mfa, err = login_and_store(user_id, req.account, req.password)
        if err:
            raise HTTPException(400, f"登录失败: {err}")
        if needs_mfa:
            return {"ok": True, "needs_mfa": True, "message": "需要两步验证码"}
        # 登录成功：记录绑定 + 后台触发首次同步
        bind_watch(user_id, req.brand, req.account, auth_token="")
        threading.Thread(target=sync_user_garmin, args=(user_id, 7), daemon=True).start()
        return {"ok": True, "needs_mfa": False, "message": f"已绑定 {req.brand}"}

    # Coros：走非官方 web API 登录（email + md5 密码，无验证码）
    if req.brand == "coros":
        if not req.account or not req.password:
            raise HTTPException(400, "Coros 需要邮箱和密码")
        ok, err = coros_login(user_id, req.account, req.password, req.region)
        if err:
            raise HTTPException(400, f"登录失败: {err}")
        bind_watch(user_id, req.brand, req.account, auth_token="")
        threading.Thread(target=sync_user_coros, args=(user_id, 7), daemon=True).start()
        return {"ok": True, "needs_mfa": False, "message": f"已绑定 {req.brand}"}

    # 其他品牌：暂只记录账号
    ok = bind_watch(user_id, req.brand, req.account, auth_token=req.password)
    if not ok:
        raise HTTPException(500, "绑定失败")
    return {"ok": True, "needs_mfa": False, "message": f"已绑定 {req.brand}"}


@app.post("/api/watch/bind-mfa")
async def api_bind_watch_mfa(req: WatchMfaRequest, user_id: int = Depends(get_current_user)):
    """两步登录第二步：提交验证码完成 Garmin 绑定。"""
    if user_id == 0:
        raise HTTPException(401, "请先登录")
    if not req.mfa_code.strip():
        raise HTTPException(400, "验证码不能为空")
    ok, err, account = complete_mfa(user_id, req.mfa_code.strip())
    if not ok:
        raise HTTPException(400, err)
    # 记录绑定 + 后台触发首次同步
    if account:
        bind_watch(user_id, "garmin", account, auth_token="")
    threading.Thread(target=sync_user_garmin, args=(user_id, 7), daemon=True).start()
    return {"ok": True, "needs_mfa": False, "message": "已绑定 Garmin"}


@app.delete("/api/watch/unbind")
async def api_unbind_watch(brand: str, user_id: int = Depends(get_current_user)):
    """解绑手表。"""
    if user_id == 0:
        raise HTTPException(401, "请先登录")
    ok = unbind_watch(user_id, brand)
    if not ok:
        raise HTTPException(404, f"未找到 {brand} 绑定")
    return {"ok": True, "message": f"已解绑 {brand}"}


class WatchToggleRequest(BaseModel):
    brand: str
    enabled: bool


@app.post("/api/watch/toggle")
async def api_toggle_watch(req: WatchToggleRequest, user_id: int = Depends(get_current_user)):
    """开关设备：显示 / 关闭（不显示数据）。"""
    if user_id == 0:
        raise HTTPException(401, "请先登录")
    ok = set_watch_enabled(user_id, req.brand, req.enabled)
    if not ok:
        raise HTTPException(404, f"未找到 {req.brand} 绑定")
    return {"ok": True, "enabled": req.enabled, "message": f"{req.brand} 已{'显示' if req.enabled else '关闭'}"}


@app.get("/api/watch/list")
async def api_watch_list(user_id: int = Depends(get_current_user)):
    """已绑定的手表列表。"""
    if user_id == 0:
        return []
    return get_watch_bindings(user_id)


class WatchSelectRequest(BaseModel):
    brand: str


@app.post("/api/watch/select")
async def api_select_watch(req: WatchSelectRequest, user_id: int = Depends(get_current_user)):
    """单选设备：只启用一个品牌，其他关闭。"""
    if user_id == 0:
        raise HTTPException(401, "请先登录")
    ok = select_watch(user_id, req.brand)
    if not ok:
        raise HTTPException(404, f"未找到 {req.brand} 绑定")
    return {"ok": True, "active_brand": req.brand}


# ── 健康数据 ──

@app.get("/api/health/dashboard")
async def api_dashboard(user_id: int = Depends(get_current_user)):
    """仪表盘聚合数据。"""
    dashboard = get_dashboard(user_id)
    safety = compute_safety(user_id)
    dashboard["safety"] = safety

    now = dt.datetime.now()
    dashboard["datetime"] = now.strftime("%Y-%m-%d %H:%M")
    hour = now.hour
    if hour < 5: period = "深夜"
    elif hour < 10: period = "早晨"
    elif hour < 14: period = "上午"
    elif hour < 18: period = "下午"
    elif hour < 22: period = "傍晚"
    else: period = "夜晚"
    dashboard["period"] = period

    return dashboard


@app.get("/api/health/day/{date}")
async def api_day(date: str, user_id: int = Depends(get_current_user)):
    """某天详情。"""
    data = get_day(date, user_id)
    if not data:
        raise HTTPException(404, f"无 {date} 的数据")
    return data


@app.get("/api/health/recent/{days}")
async def api_recent(days: int = 7, user_id: int = Depends(get_current_user)):
    """最近 N 天。"""
    return get_recent(days, user_id)


@app.get("/api/health/safety")
async def api_safety(user_id: int = Depends(get_current_user)):
    """当前安全档位。"""
    return compute_safety(user_id)


@app.get("/api/health/activity/{activity_id}")
async def api_activity(activity_id: str, user_id: int = Depends(get_current_user)):
    """单次活动详情。"""
    data = get_activity(activity_id, user_id)
    if not data:
        raise HTTPException(404, f"无活动 {activity_id}")
    return data


@app.delete("/api/health/activity/{activity_id}")
async def api_delete_activity(activity_id: str, user_id: int = Depends(get_current_user)):
    """删除一条训练记录。"""
    ok = delete_activity(activity_id, user_id)
    if not ok:
        raise HTTPException(404, "记录不存在")
    return {"ok": True}


class UpdateExercisesRequest(BaseModel):
    exercises: list = []


@app.put("/api/health/activity/{activity_id}")
async def api_update_activity(activity_id: str, req: UpdateExercisesRequest, user_id: int = Depends(get_current_user)):
    """编辑力量训练的动作明细（删旧插新）。"""
    ok = update_activity_exercises(activity_id, user_id, req.exercises)
    if not ok:
        raise HTTPException(404, "记录不存在")
    return {"ok": True}


@app.get("/api/health/weather")
async def api_weather(user_id: int = Depends(get_current_user)):
    """今日天气。"""
    today = dt.date.today().isoformat()
    data = get_weather(today, user_id)
    if not data:
        raise HTTPException(404, "今日天气未同步")
    return data


@app.post("/api/health/sync")
async def api_sync_health(user_id: int = Depends(get_current_user)):
    """手动同步：当天首次全量 7 天，之后只同步当天。"""
    if user_id == 0:
        raise HTTPException(401, "请先登录")
    today = dt.date.today().isoformat()
    days = 1 if get_last_sync_date(user_id) == today else 7
    results = {}
    brands = [b["brand"] for b in get_watch_bindings(user_id) if b.get("enabled")]
    for brand in brands:
        if brand == "garmin":
            n, err = sync_user_garmin(user_id, days=days)
            results["garmin"] = {"days": n, "error": err}
        elif brand == "coros":
            n, err = sync_user_coros(user_id, days=days)
            results["coros"] = {"days": n, "error": err}
    update_last_sync_date(user_id, today)
    total = sum(v["days"] for v in results.values())
    errs = [v["error"] for v in results.values() if v["error"]]
    if errs and total == 0:
        raise HTTPException(400, errs[0])
    return {"ok": True, "days": total, "results": results}


@app.post("/api/health/sync-weather")
async def api_sync_weather(user_id: int = Depends(get_current_user), lon: float = 0.0, lat: float = 0.0):
    """手动触发天气同步。lon/lat 为 0 时用默认襄阳坐标。"""
    from weather_server import sync_weather_to_db
    if not lon or not lat:
        lon, lat = 112.14, 32.04  # 默认襄阳
    try:
        today = dt.date.today().isoformat()
        weather = sync_weather_to_db(user_id, today, lon=lon, lat=lat)
        if weather is None:
            return {"ok": False, "message": "未定位"}
        return {"ok": True, "message": f"天气已同步 {today}", "location": weather.get("location")}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@app.get("/api/chat/history")
async def api_chat_history(date: str = "", user_id: int = Depends(get_current_user)):
    """聊天历史，date 可选（YYYY-MM-DD），默认返回最近 200 条。"""
    return get_chat_history(200, user_id, date)


@app.get("/api/chat/dates")
async def api_chat_dates(user_id: int = Depends(get_current_user)):
    """近 7 天有聊天记录的日期列表（供日期下拉框）。"""
    return get_chat_dates(user_id)


@app.get("/api/user/profile")
async def api_get_profile(user_id: int = Depends(get_current_user)):
    """获取用户档案（需求8字段）。"""
    return get_user_profile(user_id)


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


@app.post("/api/user/profile")
async def api_update_profile(req: UpdateProfileRequest, user_id: int = Depends(get_current_user)):
    """更新用户档案（需求8字段）。"""
    ok = update_user_profile(user_id, req.model_dump())
    return {"ok": ok}


@app.post("/api/asr")
async def api_asr(file: UploadFile = File(...), user_id: int = Depends(get_current_user)):
    """语音转文字（StepFun ASR）。"""
    audio_bytes = await file.read()
    text = await asr_transcribe(audio_bytes, file.filename or "audio.m4a")
    if not text:
        raise HTTPException(500, "语音识别失败，请重试")
    return {"text": text}


@app.post("/api/chat/voice")
async def api_chat_voice(
    file: UploadFile = File(...),
    duration_sec: int = Form(0),
    user_id: int = Depends(get_current_user),
):
    """语音消息：存音频 + ASR 转文字 + 存语音消息 + AI 回复。"""
    from llm_client import chat as llm_chat
    audio_bytes = await file.read()

    # 1. 存音频文件
    audio_filename = f"voice_{user_id}_{int(dt.datetime.now().timestamp())}.m4a"
    audio_path = AUDIO_DIR / audio_filename
    with open(audio_path, "wb") as f:
        f.write(audio_bytes)
    audio_url = f"/static/audio/{audio_filename}"

    # 2. ASR 转文字
    text = await asr_transcribe(audio_bytes, file.filename or "audio.m4a")
    if not text:
        raise HTTPException(500, "语音识别失败，请重试")

    # 3. 存语音消息（content 存 ASR 文字，用户点"转文字"时展示）
    add_chat("user", text, user_id, msg_type="voice", audio_url=audio_url, duration_sec=duration_sec)

    # 4. AI 回复
    ctx = build_chat_context(user_id)
    history = get_chat_history(20, user_id)
    reply = await llm_chat(text, ctx, history, None)
    clean_reply, record = extract_training_record(reply)
    recorded = record is not None
    if record:
        record_training(
            user_id,
            record.get("type", "strength_training"),
            int(record.get("duration_min", 0) or 0),
            record.get("notes", ""),
            exercises=record.get("exercises"),
        )
        reply = clean_reply or reply
    add_chat("assistant", reply, user_id)

    return {"text": text, "audio_url": audio_url, "duration_sec": duration_sec, "reply": reply, "recorded": recorded}


@app.delete("/api/chat/history")
async def api_clear_chat(user_id: int = Depends(get_current_user)):
    """清空聊天历史。"""
    clear_chat_history(user_id)
    return {"ok": True}


class ChatRequest(BaseModel):
    message: str
    provider: str = ""


def extract_training_record(reply: str):
    """从 AI 回复提取 [RECORD] 训练记录标记，返回 (干净回复, 记录dict或None)。"""
    m = re.search(r"\[RECORD\](.*?)\[/RECORD\]", reply, re.DOTALL)
    if not m:
        return reply, None
    record = None
    try:
        record = json.loads(m.group(1))
    except (json.JSONDecodeError, TypeError):
        pass
    clean = reply.replace(m.group(0), "").strip()
    return clean, record


def extract_plan(reply: str):
    """从 AI 回复提取 [PLAN] 未来计划标记，返回 (干净回复, plan dict 或 None)。"""
    m = re.search(r"\[PLAN\](.*?)\[/PLAN\]", reply, re.DOTALL)
    if not m:
        return reply, None
    plan = None
    try:
        plan = json.loads(m.group(1))
    except (json.JSONDecodeError, TypeError):
        pass
    clean = reply.replace(m.group(0), "").strip()
    return clean, plan


@app.post("/api/chat")
async def api_chat(req: ChatRequest, user_id: int = Depends(get_current_user)):
    """非流式对话。

    优先走新的带工具 Agent（``garmin_coach.agent``）：模型自己决定查什么数据，
    而不是由服务端把全部数据拼进 prompt。任何环节出问题（没 key、网络、
    协议不兼容）都回退到旧的单轮实现，保证这个接口不会因为重构而不可用。
    """
    from llm_client import chat

    history = get_chat_history(20, user_id)
    add_chat("user", req.message, user_id)

    agent_result = None
    ctx: dict = {}
    try:
        from garmin_coach.agent.bridge import run_agent_chat

        agent_result = await run_agent_chat(
            req.message,
            user_id,
            history=history,
            provider_name=req.provider,
        )
        reply = agent_result.text
        log.info(
            "[agent] user=%s turns=%s tools=%s chars=%s",
            user_id,
            agent_result.turns,
            agent_result.tools_used,
            len(reply),
        )
    except Exception as exc:  # noqa: BLE001 - 重构期的安全网，任何异常都回退
        log.warning("[agent] 失败，回退旧实现: %s: %s", exc.__class__.__name__, exc)
        ctx = build_chat_context(user_id)
        reply = await chat(req.message, ctx, history, req.provider or None)

    # 训练记录：新路径靠 record_training 工具直接落库；
    # 这里仍保留旧的 [RECORD] 标记解析，作为回退路径和模型不听话时的兜底。
    recorded = bool(
        agent_result
        and any(c.name == "record_training" for c in agent_result.tool_calls)
    )
    clean_reply, record = extract_training_record(reply)
    if record:
        record_training(
            user_id,
            record.get("type", "strength_training"),
            int(record.get("duration_min", 0) or 0),
            record.get("notes", ""),
            exercises=record.get("exercises"),
        )
        recorded = True
        reply = clean_reply or reply

    # 未来计划：同上，新路径用 save_plan 工具，旧路径用 [PLAN] 标记
    clean_reply, plan = extract_plan(reply)
    if plan:
        save_plan(user_id, json.dumps(plan, ensure_ascii=False))
        reply = clean_reply or reply

    add_chat("assistant", reply, user_id)

    return {"reply": reply, "context": ctx, "recorded": recorded}


@app.get("/api/llm/providers")
async def api_providers():
    """列出可用的 LLM 提供商。"""
    result = {}
    for name, conf in PROVIDERS.items():
        result[name] = {
            "name": conf["name"],
            "model": conf["model"],
            "available": bool(conf["api_key"]),
        }
    return result


class NoteRequest(BaseModel):
    text: str
    date: str = ""


@app.post("/api/note")
async def api_add_note(req: NoteRequest):
    """记一条笔记。"""
    date_str = req.date or dt.date.today().isoformat()
    entry = add_note(date_str, req.text)
    return {"ok": True, "entry": entry}


# ═══════════════════════════════════════════════
# 手动训练记录
# ═══════════════════════════════════════════════

class RecordTrainingRequest(BaseModel):
    type: str = "strength_training"  # running / strength_training / cycling 等
    notes: str = ""                   # 训练内容描述
    duration_min: int = 0             # 时长（分钟）
    date: str = ""                    # 日期，默认今天


@app.post("/api/health/record-training")
async def api_record_training(req: RecordTrainingRequest, user_id: int = Depends(get_current_user)):
    """手动记录一次训练（通过对话或手动输入）。"""
    ok = record_training(user_id, req.type, req.duration_min, req.notes, req.date)
    if ok:
        return {"ok": True, "message": f"已记录 {req.date or dt.date.today().isoformat()} {req.type}"}
    return {"ok": False, "message": "记录失败"}


def activityLabel(typ: str) -> str:
    return {
        "running": "跑步", "strength_training": "力量训练",
        "cycling": "骑行", "walking": "步行", "swimming": "游泳",
    }.get(typ, typ)


# ═══════════════════════════════════════════════
# 推送通知 API
# ═══════════════════════════════════════════════

class PushRegisterRequest(BaseModel):
    token: str
    platform: str = "android"


@app.post("/api/push/register")
async def api_push_register(req: PushRegisterRequest):
    """注册设备推送 Token。Android App 启动时调用。"""
    if not req.token.strip():
        raise HTTPException(400, "Token 不能为空")
    ok = register_device(req.token.strip(), req.platform)
    return {"ok": ok, "token": req.token[:20] + "..."}


@app.get("/api/push/status")
async def api_push_status():
    """推送状态：Firebase 是否就绪 + 已注册设备数。"""
    tokens = get_active_tokens()
    return {
        "firebase_ready": init_firebase(),
        "active_devices": len(tokens),
    }


class PushTestRequest(BaseModel):
    title: str = "Garmin Coach"
    body: str = "测试推送 🏃"
    click_action: str = "dashboard"


@app.post("/api/push/test")
async def api_push_test(req: PushTestRequest):
    """测试推送：向所有活跃设备发一条通知。"""
    tokens = get_active_tokens()
    if not tokens:
        return {"ok": False, "message": "无活跃设备"}
    sent = 0
    for token in tokens:
        if send_push(token, req.title, req.body, req.click_action):
            sent += 1
    return {"ok": True, "sent": sent, "total": len(tokens)}


@app.post("/api/push/check")
async def api_push_check():
    """手动触发训练提醒检查（调试用）。"""
    result = check_and_notify()
    return result


# ═══════════════════════════════════════════════
# WebSocket 聊天（流式）
# ═══════════════════════════════════════════════

@app.websocket("/ws/chat")
async def ws_chat(ws: WebSocket):
    await ws.accept()
    # WebSocket 鉴权：从 query 参数取 token，或第一次消息中附带的 token
    try:
        while True:
            msg = await ws.receive_text()
            data = json.loads(msg)
            message = data.get("message", "")
            provider = data.get("provider", "")
            user_id = data.get("user_id", 0)  # WebSocket 从客户端传入

            if not message.strip():
                continue

            ctx = build_chat_context(user_id)
            history = get_chat_history(20, user_id)

            add_chat("user", message, user_id)

            # 先发上下文摘要
            await ws.send_text(json.dumps({
                "type": "context",
                "safety": ctx.get("safety"),
                "datetime": ctx.get("datetime"),
                "period": ctx.get("period"),
            }, ensure_ascii=False))

            # 流式回复
            full_reply = ""
            async for token in chat_stream(message, ctx, history, provider or None):
                full_reply += token
                await ws.send_text(json.dumps({
                    "type": "token",
                    "token": token,
                }, ensure_ascii=False))

            # 解析训练记录标记，落库
            clean_reply, record = extract_training_record(full_reply)
            if record:
                record_training(
                    user_id,
                    record.get("type", "strength_training"),
                    int(record.get("duration_min", 0) or 0),
                    record.get("notes", ""),
                    exercises=record.get("exercises"),
                )
                full_reply = clean_reply or full_reply

            # 提取未来计划 [PLAN]，落库（user_id 隔离）
            clean_reply, plan = extract_plan(full_reply)
            if plan:
                save_plan(user_id, json.dumps(plan, ensure_ascii=False))
                full_reply = clean_reply or full_reply

            add_chat("assistant", full_reply, user_id)

            await ws.send_text(json.dumps({
                "type": "done",
                "final_reply": full_reply,
            }, ensure_ascii=False))

    except WebSocketDisconnect:
        pass
    except Exception as e:
        await ws.send_text(json.dumps({
            "type": "error",
            "message": str(e),
        }, ensure_ascii=False))


# ═══════════════════════════════════════════════
# 静态文件
# ═══════════════════════════════════════════════

@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")


# 挂载静态文件（必须在最后）
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ═══════════════════════════════════════════════
# 启动
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    from apscheduler.schedulers.background import BackgroundScheduler

    init_db()
    init_firebase()
    print(f"✅ 数据库就绪")

    # 定时推送检查：每天 8:07 和 19:07
    scheduler = BackgroundScheduler()
    scheduler.add_job(check_and_notify, "cron", hour=8, minute=7, id="push_morning")
    scheduler.add_job(check_and_notify, "cron", hour=19, minute=7, id="push_evening")
    scheduler.add_job(sync_all_users, "cron", hour=2, minute=7, id="garmin_sync_daily", kwargs={"days": 7})
    scheduler.add_job(sync_all_coros_users, "cron", hour=2, minute=17, id="coros_sync_daily", kwargs={"days": 7})
    scheduler.add_job(cleanup_old_chat, "cron", hour=3, minute=7, id="chat_cleanup", kwargs={"days": 7})
    scheduler.start()
    print(f"🔔 推送定时任务已启动（每天 8:07 / 19:07）")

    print(f"🚀 启动 Garmin Coach 服务: http://localhost:8766")
    uvicorn.run(app, host="0.0.0.0", port=8766, log_level="info")