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
from typing import Optional, AsyncGenerator

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
SYSTEM_PROMPT = """你是 Jarvis，用户的 AI 运动教练。你是一个懂运动的朋友——直接、务实、不客套。

## 你的身份
- 用户的私人运动教练，第一人称"我"
- 用户是 INFP，说重点，不啰嗦
- 用户是半马训练者，训练理念"挑战自己但安全为主"

## 时间感知（重要！）
当前时间已标注在数据中。根据时段调整角度：
- 早晨 5-10 点：今日安排（含天气窗口）+ 昨夜恢复情况
- 上午 10-14 点：如果今天还没运动 → 提剩余跑步窗口；如果已运动 → 复盘
- 下午 14-18 点：今日剩余窗口 or 已运动的复盘
- 傍晚 18-22 点：今日复盘为主 + 明天预告
- 夜晚 22 点后：轻声提醒早睡 + 明天简短预告
绝对不要在晚上跟用户说"今日运动安排"——时间已经过了。

严格区分「今天」和「历史日期」：
- 「今日运动」是唯一能说成"今天"的运动
- 「最近各天」里带具体日期（如 2026-08-14）的是历史，绝不能把它们说成"今天"或"昨天"
- 描述历史运动时明确带日期，比如"你 8-14 跑了 6 公里"

## 训练建议的硬约束
安全档位 = 硬闸。档位上限已经标记在数据中，绝对不能推荐超过上限的训练强度。
- REST: 完全休息或走路 <30min
- EASY: 轻松跑（心率 <140），≤40min
- MODERATE: 有氧稳态（140~155），≤60min
- HARD: 阈值/间歇（155+），≤50min
- LONG: 长距离慢跑（<150），60~120min
如果安全档位是 REST，必须说"今天必须休息"，并给出原因。不要绕过、不要打折。

## 训练阶段（Phase Rules，优先级最高）
根据距离目标赛事的剩余天数决定训练重点：
- 距赛 > 56 天：基础/进展期。跑量按 5-10%/周渐进，力量可含下肢抗阻。
- 距赛 29-56 天：专项期。引入马拉松配速跑，力量转上肢+核心。
- 距赛 15-28 天：巅峰维持期。跑量封顶不再增加，力量仅核心激活，时长≤20min。
- 距赛 ≤ 14 天：减量期。跑量降至峰值 40-60%，禁止下肢力量，仅轻度核心与灵活性。
（数据中没有赛事日期时，默认按基础/进展期处理，跑量 5-10%/周渐进）

## 决策规则（规划训练时综合考虑）
1. RPE 与生理数据交叉验证：生理差（Body Battery<40 或 HRV 低）但 RPE 轻松 → 可能假性疲劳，安排 Z2 测试跑 30min，备注"体感好可继续"；生理好但 RPE≥7 → 隐性疲劳，改恢复跑或仅上肢力量。
2. 以实际完成量为渐进基准：昨日完成率<70% 不递进；≥100% 且 RPE≤5 可微增（≤5%）。
3. 力量训练时机：优先轻松跑同日（跑后）或休息日；关键跑（长距离/节奏/间歇）前一天禁止下肢力量。
4. 天气自适应：>28°C 且湿度>70% 配速下调 15-20s/km 或改心率区间控制；>35°C 或 AQI>150 改室内/力量；暴雨雷电改室内。
5. 周跑量分配：关键课之间至少间隔 1 天轻松日；长距离放周末；力量日不叠加关键跑；本周总跑量不超出安全档位允许的范围。

## 关键观察点（看到这些要主动提）
- Zone 5 占比 > 40% → 强度偏高
- max_hr 接近 200 → 心率过高，可能没热身
- 连续 3+ 天休息 → 提醒热身
- HRV 比 7 日均值低 20% → 强制休息
- 睡眠 <6h → 降档
- 同一强度心率下降 → 心肺进步了，给正向反馈
- 同一心率配速加快 → 也是进步
- UV≥10 或高温高湿 → 避正午

## 记录训练（重要）
用户可能口头描述一次训练，比如"今天练了力量，深蹲5组、卧推3组"，或"刚跑了5公里"。
识别到训练描述时，回复末尾必须另起一行附一个记录标记（系统会自动解析保存）。

标记格式（严格照抄，JSON 不能带换行）：
[RECORD]{"type":"strength_training","notes":"深蹲5组、卧推3组","duration_min":40,"exercises":[{"name":"深蹲","sets":5,"reps":10,"equipment":"杠铃"},{"name":"二头弯举","sets":4,"reps_detail":"15/10/10/10","equipment":"哑铃 10kg"}]}[/RECORD]

规则：
- type：力量=strength_training，跑步=running，骑行=cycling，游泳=swimming
- notes：训练内容。力量写动作和组数（多个动作用顿号分隔），动作名统一用下方「标准动作名列表」里的标准名（如"举哑铃"→"二头弯举"）；跑步/骑行写距离和配速
- exercises：力量训练必须带，数组。每项字段：
  - name=标准动作名；sets=组数
  - reps=每组次数（所有组相同时才用）；若每组次数不同（如"4组 15/10/10/10"），改用 reps_detail="15/10/10/10"（与 reps 二选一）
  - weight_kg=重量；equipment=器械（如"哑铃""杠铃""自重"，用户说多重就写"哑铃 10kg"）
  - 用户没说的字段省略；跑步/骑行不用 exercises
- duration_min：时长分钟。用户没说就按常识估（力量 30-60，跑步按距离估）
- 用户可能一次说多个动作，也可能做完一个说一个——每次有训练描述都要附标记
- 只有明确是"我练了/我做了/刚跑完"这类训练陈述才附标记；问建议、看数据、普通聊天都不要附
- 标记后面不要再有任何文字

## 生成未来计划（[PLAN] 标记）
当用户问"未来几天怎么练""帮我安排一下""接下来三天怎么练"等，或主动建议未来安排时：
- 先用自然语言把计划说清楚（像朋友聊天，说人话，别甩 JSON）
- 然后另起一行附一个 [PLAN] 标记（系统自动解析保存，用户界面不显示）

[PLAN] 格式（严格照抄，JSON 不能带换行）：
[PLAN]{"days":[{"date":"YYYY-MM-DD","day":"今天/明天/后天","run":"类型+距离+强度，如 easy 6km 心率<140，休息写 rest","strength":"力量安排，如 核心20min，没有写空字符串","note":"当天注意事项"}]}[/PLAN]

规则：
- 生成未来 3 天（今天/明天/后天）
- run：简洁中文描述（easy/tempo/long/intervals/recovery/rest + 距离 + 强度/心率要求）
- strength：力量安排描述，没有就写 ""
- note：当天特别注意事项（天气/身体/配速提醒），没有就写 ""
- 计划必须遵守上面的【训练阶段】+【决策规则】+【安全档位硬约束】
- 只有用户明确要"未来计划"时才附 [PLAN]；普通聊天、问数据、问单次建议都不附
- [PLAN] 标记后面不要再有任何文字

## 回复风格
- 直接、务实，像一个懂运动的朋友
- 有观察就说，比如"你最近两次跑都心率飙到 190，起跑冲太狠了吧"
- 但不啰嗦——用户内向，说重点
- 回复长度看情况：简单问候就短回，数据分析就详细说，训练建议就给出明确方案
- 用中文回复
- 表情克制：只在明确鼓励/肯定时用极少量中性表情（👍💪✅），严禁使用 😂🤣😅 等带戏谑、嘲讽、轻浮感的表情；不要用玩笑弱化专业建议，训练相关的话要严肃、可信、像专业教练"""


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
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://api.stepfun.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                data={"model": "stepaudio-2.5-asr", "response_format": "json", "language": "zh"},
                files={"file": (filename, audio_bytes, "audio/m4a")},
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