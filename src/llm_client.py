"""
LLM 客户端 —— 统一的 AI 调用层，支持 DeepSeek / StepFun / Ark。
不依赖 Claude，直接用各家 API。
"""
import sys
import os
import json
import httpx
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
from typing import AsyncGenerator

# 加载 .env：优先项目目录（服务器 ~/garmin-coach/.env），fallback 本地 study/.env
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
if not ENV_PATH.exists():
    # 兼容旧的本地布局：<用户主目录>/study/.env（不硬编码用户名）
    ENV_PATH = (
        Path(os.environ.get("GARMIN_STUDY_DIR") or (Path.home() / "study")) / ".env"
    )
if ENV_PATH.exists():
    with open(ENV_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())

# ── 模型配置 ──
PROVIDERS = {
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/chat/completions",
        "api_key": os.environ.get("DEEPSEEK_API_KEY", ""),
        "model": "deepseek-v4-flash",
        "max_tokens": 4096,
    },
    "stepfun": {
        "name": "StepFun",
        "base_url": "https://api.stepfun.com/v1/chat/completions",
        "api_key": os.environ.get("STEPFUN_API_KEY", ""),
        "model": "step-3.7-flash",
        "max_tokens": 4096,
    },
    "ark": {
        "name": "Ark",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
        "api_key": os.environ.get("ARK_API_KEY", ""),
        "model": "deepseek-v3-250324",
        "max_tokens": 4096,
    },
}

DEFAULT_PROVIDER = "deepseek"

# ── System Prompt ──
# 提示词唯一来源：garmin_coach/agent/prompts.py
# 本文件只保留「吐 [RECORD]/[PLAN] 标记」那套输出约定；共享的领域规则都在那边。
from garmin_coach.agent.prompts import LEGACY_PROMPT

# 时间由 build_chat_context 注入，这里不再拼时间，避免模块导入时把时间戳冻住
SYSTEM_PROMPT = LEGACY_PROMPT


def get_provider(name: str = None) -> dict:
    """获取提供商配置，自动 fallback。"""
    name = name or DEFAULT_PROVIDER
    if name not in PROVIDERS:
        name = DEFAULT_PROVIDER
    provider = PROVIDERS[name]
    if not provider["api_key"]:
        # fallback 到第一个有 key 的
        for pname, pconf in PROVIDERS.items():
            if pconf["api_key"]:
                return pconf
    return provider


def build_context(data: dict) -> str:
    """把仪表盘数据拼成 LLM 能理解的上下文。"""
    parts = []

    # 基本信息
    now = data.get("datetime", "")
    period = data.get("period", "")
    parts.append(f"当前时间: {now} ({period})")

    # 安全档位
    safety = data.get("safety") or {}
    if safety:
        parts.append(f"\n## 安全档位（硬约束）")
        parts.append(f"- 上限: {safety.get('max_tier')}（{safety.get('tier_description', '')}）")
        blockers = safety.get("blockers") or []
        if blockers:
            parts.append(f"- 降档原因: {'; '.join(blockers)}")
        notes = safety.get("notes") or []
        if notes:
            parts.append(f"- 注意: {'; '.join(notes)}")

    # 用户训练档案（2026-08-20 新增：驱动训练阶段判断 + 比赛倒计时）
    profile = data.get("profile") or {}
    if profile:
        parts.append("\n## 用户训练档案")
        if profile.get("goal"):
            goal_label = {"half_marathon": "半马", "full_marathon": "全马", "shaping": "塑形"}.get(profile["goal"], profile["goal"])
            target = f"，目标成绩 {profile['goal_target']}" if profile.get("goal_target") else ""
            parts.append(f"- 训练目标: {goal_label}{target}")
        ttypes = profile.get("training_types") or []
        if ttypes:
            type_label = {"marathon": "全程马拉松", "half_marathon": "半程马拉松"}
            parts.append(f"- 训练类型: {' / '.join(type_label.get(t, t) for t in ttypes)}")
        races = profile.get("upcoming_races") or []
        if races:
            parts.append(f"- 未来比赛 ({len(races)} 场):")
            for r in races[:5]:
                parts.append(f"  · {r['name']} @ {r['date']}（距今 {r['days_until']} 天）")
        if profile.get("soonest_race_days") is not None:
            parts.append(f"- 距最近一场比赛: {profile['soonest_race_days']} 天")

    # 今日数据
    today = data.get("today") or {}
    if today:
        parts.append(f"\n## 今日数据")
        recovery = today.get("recovery") or {}
        if recovery.get("hrv"):
            parts.append(f"- HRV: {recovery.get('hrv')} ({recovery.get('hrv_status', '?')})")
        if recovery.get("resting_hr"):
            parts.append(f"- 静息心率: {recovery.get('resting_hr')}")
        if recovery.get("body_battery_high"):
            parts.append(f"- Body Battery: {recovery.get('body_battery_low')}→{recovery.get('body_battery_high')}")
        sleep = today.get("sleep") or {}
        if sleep.get("hours"):
            parts.append(f"- 睡眠: {sleep.get('hours')}h, 得分 {sleep.get('score', '?')}, 深睡 {sleep.get('deep_min', '?')}min")
        if recovery.get("readiness"):
            parts.append(f"- 训练准备度: {recovery.get('readiness')} ({recovery.get('readiness_level', '?')})")
        fitness = today.get("fitness") or {}
        if fitness.get("training_status"):
            parts.append(f"- 训练状态: {fitness.get('training_status')}")
        if fitness.get("acwr_ratio"):
            parts.append(f"- ACWR: {fitness.get('acwr_ratio')}")
        acts = today.get("activities") or []
        if acts:
            parts.append(f"- 今日运动: {len(acts)} 次")
            for a in acts:
                parts.append(f"  · {a.get('type')}: {a.get('distance_km')}km, {a.get('duration_min')}min, 心率{a.get('avg_hr')}/{a.get('max_hr')}")
        else:
            parts.append("- 今日尚未运动")
        weather = today.get("weather") or {}
        if weather:
            parts.append(f"- 天气: {weather.get('desc')}, {weather.get('low')}~{weather.get('high')}°C, AQI {weather.get('aqi')}")

    # 最近 7 天汇总
    recent = data.get("recent_7d") or {}
    if recent:
        parts.append(f"\n## 最近 7 天汇总")
        parts.append(f"- 跑量: {recent.get('total_distance_km', 0)}km, {recent.get('run_count', 0)} 次跑步")
        parts.append(f"- 平均 HRV: {recent.get('avg_hrv')}, 平均静息心率: {recent.get('avg_resting_hr')}, 平均睡眠: {recent.get('avg_sleep_h')}h")

    # 各天详情
    daily_stats = data.get("daily_stats") or []
    if daily_stats:
        parts.append(f"\n## 最近各天")
        for d in daily_stats:
            acts_str = ""
            for a in d.get("activities") or []:
                acts_str += f" [{a.get('type')}: {a.get('distance_km')}km, {a.get('duration_min')}min]"
            parts.append(f"- {d['date']} 周{d.get('weekday','')}: HRV{d.get('hrv')} 静息{d.get('resting_hr')} 睡眠{d.get('sleep_hours')}h{acts_str}")

    # 标准动作名（供力量训练归一化）
    std = data.get("standard_exercises") or []
    if std:
        parts.append("\n## 标准动作名列表（记录力量训练时动作统一用这些名字）")
        parts.append("、".join(std))

    # 已生成的未来计划（供 AI 参考，安排今日时优先考虑）
    plan = data.get("plan") or {}
    days = plan.get("days") or []
    if days:
        parts.append("\n## 已生成的未来训练计划（你之前定好的，安排今日时优先参考）")
        for d in days:
            run = d.get("run") or "休息"
            strength = d.get("strength") or ""
            note = d.get("note") or ""
            parts.append(f"- {d.get('date', '')} {d.get('day', '')}：跑步 {run}；力量 {strength or '无'}；备注 {note}")

    return "\n".join(parts)


async def chat_stream(
    message: str,
    context_data: dict,
    history: list[dict] = None,
    provider_name: str = None,
) -> AsyncGenerator[str, None]:
    """
    流式对话。边生成边返回 token。
    """
    provider = get_provider(provider_name)

    if not provider["api_key"]:
        yield f"❌ {provider['name']} API Key 未配置，请在 study/.env 中设置"
        return

    context = build_context(context_data)
    system_msg = SYSTEM_PROMPT + "\n\n---\n以下是用户的健康数据，基于这些数据回复：\n\n" + context

    messages = [{"role": "system", "content": system_msg}]
    if history:
        for h in history[-20:]:
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": message})

    payload = {
        "model": provider["model"],
        "messages": messages,
        "max_tokens": provider["max_tokens"],
        "temperature": 0.7,
        "stream": True,
    }

    headers = {
        "Authorization": f"Bearer {provider['api_key']}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        async with client.stream(
            "POST", provider["base_url"], json=payload, headers=headers
        ) as response:
            if response.status_code != 200:
                body = await response.aread()
                yield f"❌ {provider['name']} API 错误 ({response.status_code}): {body.decode()[:500]}"
                return

            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                        delta = (
                            chunk.get("choices", [{}])[0]
                            .get("delta", {})
                            .get("content", "")
                        )
                        if delta:
                            yield delta
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue


async def chat(
    message: str,
    context_data: dict,
    history: list[dict] = None,
    provider_name: str = None,
) -> str:
    """非流式对话，返回完整回复。"""
    provider = get_provider(provider_name)

    if not provider["api_key"]:
        return f"❌ {provider['name']} API Key 未配置"

    context = build_context(context_data)
    system_msg = SYSTEM_PROMPT + "\n\n---\n以下是用户的健康数据，基于这些数据回复：\n\n" + context

    messages = [{"role": "system", "content": system_msg}]
    if history:
        for h in history[-20:]:
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": message})

    payload = {
        "model": provider["model"],
        "messages": messages,
        "max_tokens": provider["max_tokens"],
        "temperature": 0.7,
        "stream": False,
    }

    headers = {
        "Authorization": f"Bearer {provider['api_key']}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(provider["base_url"], json=payload, headers=headers)
        if resp.status_code != 200:
            return f"❌ {provider['name']} API 错误 ({resp.status_code}): {resp.text[:500]}"

        data = resp.json()
        return data["choices"][0]["message"]["content"]


async def asr_transcribe(audio_bytes: bytes, filename: str = "audio.m4a") -> str:
    """调 StepFun ASR 转写音频，返回文字（失败返回空字符串）。"""
    api_key = PROVIDERS.get("stepfun", {}).get("api_key", "")
    if not api_key:
        return ""
    # 按扩展名给正确的 MIME —— Web 端录的是 wav，Android 是 m4a
    mime = {
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".webm": "audio/webm",
        ".ogg": "audio/ogg",
    }.get(Path(filename).suffix.lower(), "audio/m4a")
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://api.stepfun.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                data={"model": "stepaudio-2.5-asr", "response_format": "json", "language": "zh"},
                files={"file": (filename, audio_bytes, mime)},
            )
            if resp.status_code != 200:
                return ""
            return resp.json().get("text", "")
    except Exception:
        return ""


# ── 测试 ──
if __name__ == "__main__":
    import asyncio

    async def test():
        provider = get_provider()
        print(f"使用: {provider['name']} ({provider['model']})")
        print(f"Key: {'✅ 已配置' if provider['api_key'] else '❌ 未配置'}")

        # 简单测试
        ctx = {
            "datetime": "2026-07-14 20:30",
            "period": "傍晚",
            "safety": {"max_tier": "MODERATE", "tier_description": "有氧稳态"}
        }
        print("\n测试回复:")
        reply = await chat("今晚跑不跑？", ctx)
        print(reply)

    asyncio.run(test())