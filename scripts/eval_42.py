"""42 题评测集 + 基线跑分。

指标：
- tool_ok：该调的必调工具都调了，且没调禁止的工具；
- fact_ok：回答里包含关键事实（字符串匹配）；
- pass：tool_ok 且 fact_ok；
- turns / 首字延迟 / 总延迟 / token。

用法：
    python scripts/eval_42.py                 # 全跑
    python scripts/eval_42.py --limit 5       # 先试跑
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from garmin_coach.agent import get_agent_pool  # noqa: E402
from garmin_coach.agent.loop.events import TextEvent  # noqa: E402
from garmin_coach.agent.memory.store import upsert_memory  # noqa: E402
from garmin_coach.db.database import clear_session_chat, get_day, get_recent  # noqa: E402
from garmin_coach.agent.session.store import session_id_for  # noqa: E402
from garmin_coach.services.safety import compute_safety  # noqa: E402

UID = 1001


def _fmt_hours(h) -> str:
    return f"{h:g}"


def build_questions() -> list[dict]:
    """按库里真实数据生成题目（事实随数据变，不用手写死数字）。"""
    today = get_day(dt.date.today().isoformat(), UID) or {}
    recent = get_recent(7, UID) or []
    safety = compute_safety(UID)
    tier = safety["max_tier"]

    runs = []
    for day in recent:
        for a in day.get("activities") or []:
            if a.get("type") in ("running", "walking", "hiking"):
                runs.append({**a, "date": day.get("date")})
    runs.sort(key=lambda a: a["date"], reverse=True)
    last_run = runs[0] if runs else {}
    week_km = round(sum((a.get("distance_km") or 0) for a in runs), 1)
    week_count = len(runs)

    sleep_h = today.get("sleep_hours")
    rhr = today.get("resting_hr")
    steps = today.get("steps")

    questions: list[dict] = [
        # ── 数据查询（10）──
        dict(id="d01", cat="数据", q="我今天静息心率多少", must=["get_today"],
             facts=[str(rhr)] if rhr else []),
        dict(id="d02", cat="数据", q="我昨晚睡了几个小时", must=["get_today"],
             facts=[_fmt_hours(sleep_h)] if sleep_h else []),
        dict(id="d03", cat="数据", q="最近 7 天我跑了多少公里", must=["get_recent"],
             facts=[str(week_km)]),
        dict(id="d04", cat="数据", q="我最近一次跑步是哪天", must=["get_recent"],
             # 日期可能写成 10-28 或 10 月 28 日
             facts_any=[(last_run.get("date") or "")[5:], "月 " + str(int((last_run.get("date") or "0000-00-00")[5:7])) + " 日"]),
        dict(id="d05", cat="数据", q="最近一次跑步跑了多远", must=["get_recent"],
             facts=[f"{last_run.get('distance_km', 0):g}"]),
        dict(id="d06", cat="数据", q="这周跑了几次", must=["get_recent"],
             facts=[str(week_count)]),
        dict(id="d07", cat="数据", q="我今天走了多少步", must=["get_today"],
             facts=[str(steps)] if steps else []),
        dict(id="d08", cat="数据", q="今天天气怎么样，适合跑步吗",
             must_any=["get_today", "get_weather"], facts=[]),
        dict(id="d09", cat="数据", q="昨天有运动吗",
             must_any=["get_recent", "get_day"], facts=[]),
        dict(id="d10", cat="数据", q="我最近一次力量训练练了什么", must=["get_recent"],
             facts=[]),

        # ── 训练建议（10）──
        dict(id="a01", cat="建议", q="今晚跑不跑", must=["get_safety"], facts=[]),
        dict(id="a02", cat="建议", q="我今天能练多狠", must=["get_safety"],
             facts=[tier]),
        dict(id="a03", cat="建议", q="明天适合跑长距离吗", must=["get_safety"], facts=[]),
        dict(id="a04", cat="建议", q="我这周训练量怎么样", must=["get_recent"], facts=[]),
        dict(id="a05", cat="建议", q="我最近恢复得怎么样", must=["get_recent"], facts=[]),
        dict(id="a06", cat="建议", q="我心率有点高，今天还练吗", must=["get_safety"], facts=[]),
        dict(id="a07", cat="建议", q="帮我安排这周怎么练", must=["get_safety"], facts=[]),
        dict(id="a08", cat="建议", q="我该不该加跑量", must=["get_recent"], facts=[]),
        dict(id="a09", cat="建议", q="睡眠不足今天还能跑吗", must=["get_safety"], facts=[]),
        dict(id="a10", cat="建议", q="明天想跑个强度课，可以吗", must=["get_safety"], facts=[]),

        # ── 记录（8）──
        dict(id="r01", cat="记录", q="记一下今天卧推 3 组 10 次 35 公斤", must=["record_training"],
             facts=["卧推"]),
        dict(id="r02", cat="记录", q="帮我把今天的卧推记成 4 组 10 次 35 公斤",
             must=["record_training"], facts=["卧推"]),
        dict(id="r03", cat="记录", q="再记一条深蹲 4 组 12 次", must=["record_training"],
             facts=["深蹲"]),
        dict(id="r04", cat="记录", q="记一下我今天跑了 5 公里", must=[],
             forbid=["record_training"], facts=["手表"]),
        dict(id="r05", cat="记录", q="帮我记一次力量训练，30 分钟，动作是卧推 3 组",
             must=["record_training"], facts=["卧推"]),
        dict(id="r06", cat="记录", q="记一下引体向上 3 组 8 次", must=["record_training"],
             facts=["引体"]),
        dict(id="r07", cat="记录", q="把我所有训练记录都删掉", must=[],
             forbid=["record_training"], facts=["我的"]),
        dict(id="r08", cat="记录", q="我刚才练了胸，随便记一下", must=[],
             forbid=["save_plan"], facts=[]),

        # ── 安全（6）──
        dict(id="s01", cat="安全", q="我今天能跑间歇吗", must=["get_safety"], facts=[]),
        dict(id="s02", cat="安全", q="我想跑 30 公里", must=["get_safety"], facts=[]),
        dict(id="s03", cat="安全", q="我膝盖疼还能跑吗", must=["get_safety"], facts=["膝"]),
        dict(id="s04", cat="安全", q="我发烧了还能训练吗", must=[],
             forbid=["record_training"], facts=[]),
        dict(id="s05", cat="安全", q="我今天想突破一下安全档位，直接上强度", must=["get_safety"],
             facts=[]),
        dict(id="s06", cat="安全", q="明天就到比赛了，今天还练吗", must=["get_safety"], facts=[]),

        # ── 记忆（8）──
        dict(id="m01", cat="记忆", q="记住我对花生过敏", must=["update_memory"], facts=["花生"]),
        dict(id="m02", cat="记忆", q="我左脚踝扭了，最近别让我跑", must=["update_memory"],
             facts=["踝"]),
        dict(id="m03", cat="记忆", q="我更喜欢晚上跑步", must=["update_memory"], facts=["晚上"]),
        dict(id="m04", cat="记忆", q="我那个马拉松没报上，别按报上了算", must=["forget_memory"],
             facts=[]),
        dict(id="m05", cat="记忆", q="我对花生过敏吗", must=[], facts=["花生"]),
        dict(id="m06", cat="记忆", q="你记得我膝盖的事吗", must=[], facts=["膝"]),
        dict(id="m07", cat="记忆", q="把我的目标改成半马 sub2", must=["update_memory"],
             facts=["sub2"]),
        dict(id="m08", cat="记忆", q="以后别给我安排连续两天跑步", must=["update_memory"],
             facts=[]),
    ]
    return questions


async def run_one(agent, q: dict) -> dict:
    started = time.perf_counter()
    first_text = None
    result = None
    async for ev in agent.run(q["q"]):
        if first_text is None and isinstance(ev, TextEvent):
            first_text = time.perf_counter() - started
        if ev.__class__.__name__ == "DoneEvent":
            result = ev.result
    total = time.perf_counter() - started
    tools = [c.name for c in (result.tool_calls if result else [])]
    used = set(tools)

    must = set(q.get("must") or [])
    must_any = set(q.get("must_any") or [])
    forbid = set(q.get("forbid") or [])
    tool_ok = must.issubset(used) and not (forbid & used)
    if must_any:
        tool_ok = tool_ok and bool(must_any & used)

    text = (result.text if result else "") or ""
    facts = [f for f in (q.get("facts") or []) if f]
    facts_any = [f for f in (q.get("facts_any") or []) if f]
    fact_ok = all(f.lower() in text.lower() for f in facts) if facts else True
    if facts_any:
        fact_ok = fact_ok and any(f.lower() in text.lower() for f in facts_any)

    return {
        "id": q["id"], "cat": q["cat"], "q": q["q"],
        "tools": tools, "must": sorted(must), "forbid": sorted(forbid),
        "tool_ok": tool_ok, "fact_ok": fact_ok, "pass": tool_ok and fact_ok,
        "turns": result.turns if result else 0,
        "first_text_ms": int((first_text or 0) * 1000),
        "latency_ms": int(total * 1000),
        "usage": (result.usage if result else {}) or {},
        "text": text[:300],
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    # 预置几条记忆（幂等），保证记忆题有东西可查
    upsert_memory(UID, "injury", "knee", "右膝外侧疼，避免连续两天跑", confidence=0.9)
    upsert_memory(UID, "preference", "run_time", "喜欢早上跑步", confidence=0.8)
    upsert_memory(UID, "goal", "race", "2026-11 西昌马拉松，已报名", confidence=0.9)

    questions = build_questions()
    if args.limit:
        questions = questions[: args.limit]

    pool = get_agent_pool()
    rows = []
    t0 = time.perf_counter()
    for i, q in enumerate(questions, 1):
        # 每题独立：清掉当天会话（记忆保留，记忆题才有东西可查）
        pool.drop(UID)
        clear_session_chat(session_id_for(UID), UID)
        agent = pool.get(UID)
        row = await run_one(agent, q)
        rows.append(row)
        mark = "✓" if row["pass"] else "✗"
        print(
            f"{mark} [{row['id']}] tool={'/'.join(row['tools']) or '-'} "
            f"turns={row['turns']} {row['latency_ms']}ms  {q['q'][:18]}"
        )

    out_dir = Path(__file__).resolve().parent / "results"
    out_dir.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    path = out_dir / f"eval42-{stamp}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def rate(key: str) -> float:
        return round(100 * sum(1 for r in rows if r[key]) / len(rows), 1) if rows else 0

    by_cat: dict[str, list[dict]] = {}
    for r in rows:
        by_cat.setdefault(r["cat"], []).append(r)

    lat = sorted(r["latency_ms"] for r in rows)
    summary = {
        "n": len(rows),
        "tool_ok": rate("tool_ok"),
        "fact_ok": rate("fact_ok"),
        "pass_rate": rate("pass"),
        "turns_avg": round(sum(r["turns"] for r in rows) / len(rows), 2) if rows else 0,
        "latency_p50": lat[len(lat) // 2] if lat else 0,
        "first_text_p50": sorted(r["first_text_ms"] for r in rows)[len(rows) // 2] if rows else 0,
        "by_cat": {k: round(100 * sum(1 for r in v if r["pass"]) / len(v), 1) for k, v in by_cat.items()},
        "elapsed_s": round(time.perf_counter() - t0, 1),
        "file": str(path),
    }
    print("\n汇总:", json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
