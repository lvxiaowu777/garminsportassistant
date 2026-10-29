"""60 题评测：在 42 题基础上加「上下文 / 时间 / 数据过期 / 计划」四类。

用法：
    python scripts/eval_60.py
    python scripts/eval_60.py --limit 8
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from eval_42 import build_questions as build_base  # noqa: E402
from garmin_coach.agent import get_agent_pool  # noqa: E402
from garmin_coach.agent.loop.events import TextEvent  # noqa: E402
from garmin_coach.agent.memory.store import delete_memory, top_memories, upsert_memory  # noqa: E402
from garmin_coach.agent.session.store import session_id_for  # noqa: E402
from garmin_coach.db.database import add_chat, save_plan  # noqa: E402

UID = 1001


def build_extra() -> list[dict]:
    """18 道新题：上下文 6 / 时间 4 / 数据过期 4 / 计划 4。"""
    return [
        # ── 上下文（6）──
        dict(id="c01", cat="上下文", turns=["我右膝有点不舒服", "那今天还能跑吗"],
             must=["get_safety"], facts_any=["膝", "恢复"]),
        dict(id="c02", cat="上下文", turns=["我今天练了腿", "下午跑个 10 公里行吗"],
             must=["get_safety"], facts_any=["腿", "恢复", "强度"]),
        dict(id="c03", cat="上下文", turns=["我目标是半马 sub2", "那这周怎么练"],
             must=["get_safety"], facts_any=["计划", "周"]),
        dict(id="c04", cat="上下文", turns=["我昨天跑了 20 公里", "今天还能跑吗"],
             must=["get_safety"], facts_any=["恢复", "轻松", "休"]),
        dict(id="c05", cat="上下文", turns=["记一下卧推 4 组", "那深蹲呢，3 组"],
             must=["record_training"], facts=["深蹲"]),
        dict(id="c06", cat="上下文", turns=["我今天状态怎么样", "那明天呢"],
             must=["get_safety"], facts_any=["明天"]),

        # ── 时间（4，固定时刻）──
        dict(id="t01", cat="时间", time="2026-10-29T09:30",
             turns=["早上好，我今天什么时候跑合适"], must=["get_safety"],
             facts_any=["早上", "上午", "今天"]),
        dict(id="t02", cat="时间", time="2026-10-29T15:00",
             history=[("user", "我上午练了腿"), ("assistant", "记下了，下午看恢复情况。")],
             turns=["下午去跑个 10 公里合适吗"], must=["get_safety"],
             facts_any=["腿", "恢复", "心率"]),
        dict(id="t03", cat="时间", time="2026-10-29T21:30",
             turns=["现在去跑步合适吗"], must=["get_safety"],
             facts_any=["晚", "明天", "休息"]),
        dict(id="t04", cat="时间", time="2026-10-29T06:30",
             turns=["现在空腹跑可以吗"], must=["get_safety"],
             facts_any=["空腹", "早上", "可以"]),

        # ── 数据过期（4）──
        dict(id="x01", cat="过期",
             history=[("user", "今天静息心率多少"), ("assistant", "你今天静息心率是 52。")],
             turns=["现在还是 52 吗"], must=["get_today"], facts=[]),
        dict(id="x02", cat="过期",
             history=[("user", "今天状态怎么样"), ("assistant", "今天数据看着还行。")],
             turns=["你刚才查的数据是几点的"], must=["get_today"],
             facts_any=["小时", "分钟", "同步", "刚才"]),
        dict(id="x03", cat="过期", turns=["数据可能过期了，重新查一下今天的状态"],
             must=["get_today"], facts=[]),
        dict(id="x04", cat="过期",
             history=[("user", "今天跑了多少"), ("assistant", "今天还没跑。")],
             turns=["过了一小时了，再查一遍"], must=["get_today"], facts=[]),

        # ── 计划（4）──
        dict(id="p01", cat="计划", turns=["今天我计划练什么"], must=["get_profile"],
             facts_any=["跑", "力量", "练"]),
        dict(id="p02", cat="计划", turns=["把明天改成轻松跑 5 公里"], must=["save_plan"],
             facts=["明天"]),
        dict(id="p03", cat="计划", turns=["这周的计划还有效吗"], must=["get_profile"],
             facts_any=["有效", "计划", "到"]),
        dict(id="p04", cat="计划", turns=["我不喜欢胸和二头一起练，帮我排下周计划"],
             must=["update_memory", "save_plan"], memory_has=[("preference", "split", "胸")],
             facts_any=["周", "计划", "练"]),
    ]


def seed_plan() -> None:
    today = dt.date.today()
    days = []
    for i in range(7):
        days.append({
            "date": (today + dt.timedelta(days=i)).isoformat(),
            "day": f"第{i + 1}天",
            "run": "easy 6km" if i % 2 == 0 else "休息",
            "strength": "核心" if i % 2 == 0 else "无",
            "note": "",
        })
    save_plan(UID, json.dumps(days, ensure_ascii=False))


async def run_case(agent, case: dict) -> dict:
    turns = case.get("turns") or [case["q"]]
    started = time.perf_counter()
    first_text = None
    tools: list[str] = []
    text = ""
    turns_used = 0
    usage: dict = {}

    for msg in turns:
        result = None
        async for ev in agent.run(msg):
            if first_text is None and isinstance(ev, TextEvent):
                first_text = time.perf_counter() - started
            if ev.__class__.__name__ == "DoneEvent":
                result = ev.result
        turns_used += 1
        if result:
            tools.extend(c.name for c in result.tool_calls)
            text = result.text or ""
            usage = result.usage or usage

    total = time.perf_counter() - started
    used = set(tools)
    must = set(case.get("must") or [])
    must_any = set(case.get("must_any") or [])
    forbid = set(case.get("forbid") or [])
    facts = [f for f in (case.get("facts") or []) if f]
    facts_any = [f for f in (case.get("facts_any") or []) if f]

    tool_ok = must.issubset(used) and not (forbid & used)
    if must_any:
        tool_ok = tool_ok and bool(must_any & used)
    fact_ok = all(f.lower() in text.lower() for f in facts) if facts else True
    if facts_any:
        fact_ok = fact_ok and any(f.lower() in text.lower() for f in facts_any)

    mem_ok = True
    for kind, key, needle in case.get("memory_has") or []:
        hit = [
            m for m in top_memories(UID, limit=50)
            if m["kind"] == kind and (not key or key in (m["key"] or ""))
        ]
        mem_ok = mem_ok and any(needle in (m["value"] or "") for m in hit)

    return {
        "id": case["id"], "cat": case["cat"], "turns": turns,
        "tools": tools, "must": sorted(must), "forbid": sorted(forbid),
        "expect": {
            "must": sorted(must), "must_any": sorted(must_any),
            "forbid": sorted(forbid), "facts": facts,
            "facts_any": facts_any, "memory_has": case.get("memory_has") or [],
        },
        "tool_ok": tool_ok, "fact_ok": fact_ok, "memory_ok": mem_ok,
        "pass": tool_ok and fact_ok and mem_ok,
        "turns_used": turns_used,
        "first_text_ms": int((first_text or 0) * 1000),
        "latency_ms": int(total * 1000),
        "usage": usage,
        "text": text[:600],
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--extra", action="store_true", help="只跑新增的 18 题")
    parser.add_argument("--only", default="", help="只跑指定 id，逗号分隔")
    args = parser.parse_args()

    # 预置记忆 + 计划（幂等）
    upsert_memory(UID, "injury", "knee", "右膝外侧疼，避免连续两天跑", confidence=0.9)
    upsert_memory(UID, "preference", "run_time", "喜欢早上跑步", confidence=0.8)
    upsert_memory(UID, "goal", "race", "2026-11 西昌马拉松，已报名", confidence=0.9)
    delete_memory(UID, "preference", "split")   # 计划题要测"新偏好有没有写进去"
    seed_plan()

    cases = build_extra() if args.extra else build_base() + build_extra()
    if args.only:
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        cases = [c for c in cases if c["id"] in wanted]
    if args.limit:
        cases = cases[: args.limit]

    pool = get_agent_pool()
    rows = []
    t0 = time.perf_counter()
    for case in cases:
        os.environ.pop("AGENT_TIME_OVERRIDE", None)
        if case.get("time"):
            os.environ["AGENT_TIME_OVERRIDE"] = case["time"]

        # 每题独立会话：不清用户真实聊天，只写评测自己的会话
        eval_sid = f"u{UID}-eval-{case['id']}-{dt.datetime.now().strftime('%H%M%S')}"
        os.environ["SESSION_ID_OVERRIDE"] = eval_sid
        for role, content in case.get("history") or []:
            add_chat(role, content, UID, session_id=eval_sid)

        row = await run_case(pool.get(UID), case)
        rows.append(row)
        os.environ.pop("AGENT_TIME_OVERRIDE", None)
        os.environ.pop("SESSION_ID_OVERRIDE", None)
        mark = "✓" if row["pass"] else "✗"
        print(
            f"{mark} [{row['id']}] tool={'/'.join(row['tools']) or '-'} "
            f"turns={row['turns_used']} {row['latency_ms']}ms  {row['turns'][-1][:18]}"
        )

    out_dir = Path(__file__).resolve().parent / "results"
    out_dir.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    jsonl = out_dir / f"eval60-{stamp}.jsonl"
    with jsonl.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def pct(key: str) -> float:
        return round(100 * sum(1 for r in rows if r[key]) / len(rows), 1) if rows else 0

    by_cat: dict[str, list[dict]] = {}
    for r in rows:
        by_cat.setdefault(r["cat"], []).append(r)

    lat = sorted(r["latency_ms"] for r in rows)
    summary = {
        "n": len(rows),
        "pass_rate": pct("pass"),
        "tool_ok": pct("tool_ok"),
        "fact_ok": pct("fact_ok"),
        "turns_avg": round(sum(r["turns_used"] for r in rows) / len(rows), 2),
        "latency_p50": lat[len(lat) // 2] if lat else 0,
        "elapsed_s": round(time.perf_counter() - t0, 1),
        "by_cat": {k: round(100 * sum(1 for r in v if r["pass"]) / len(v), 1) for k, v in by_cat.items()},
        "failed": [r["id"] for r in rows if not r["pass"]],
    }
    print("\n汇总:", json.dumps(summary, ensure_ascii=False))

    # Markdown 报告
    md = out_dir / f"eval60-{stamp}.md"
    lines = [
        f"# 60 题评测结果（{stamp}）",
        "",
        f"- 通过率：**{summary['pass_rate']}%**（{sum(1 for r in rows if r['pass'])}/{len(rows)}）",
        f"- 工具命中：{summary['tool_ok']}%　事实命中：{summary['fact_ok']}%",
        f"- 平均轮次：{summary['turns_avg']}　总延迟 P50：{summary['latency_p50']}ms",
        f"- 耗时：{summary['elapsed_s']}s",
        "",
        "| 类别 | 通过率 |",
        "|---|---|",
    ]
    for k, v in summary["by_cat"].items():
        lines.append(f"| {k} | {v}% |")
    lines += ["", "## 失败题", "", "| id | 问题 | 工具 | 原因 |", "|---|---|---|---|"]
    for r in rows:
        if r["pass"]:
            continue
        why = []
        if not r["tool_ok"]:
            why.append(f"工具（期望 {r['must'] or '无'}，实际 {r['tools'] or '无'}）")
        if not r["fact_ok"]:
            why.append("事实缺失")
        if not r["memory_ok"]:
            why.append("记忆未写入")
        lines.append(f"| {r['id']} | {r['turns'][-1]} | {'/'.join(r['tools']) or '-'} | {'；'.join(why)} |")
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("报告:", md)


if __name__ == "__main__":
    asyncio.run(main())
