"""5 题冒烟评测：跑通"问题 → Agent → 指标"这条链路，产出第一版基线。

它不是完整评测（那是 42 题的事），只用来：
  1. 验证评测脚本本身能跑；
  2. 拿一组"改前"数字（轮次 / 延迟 / token / 工具序列）。

用法（项目根目录）：
    python scripts/smoke_eval.py
    python scripts/smoke_eval.py --user 1001 --repeat 2 --out scripts/results/x.jsonl

依赖：.env 里的 DATABASE_URL（MySQL）与 LLM key；库里要有 seed 数据。
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))          # garmin_coach 包在项目根
sys.path.insert(0, str(ROOT / "src"))  # 启动器还在 src

# 评测不连手表：关掉"对话前自动同步"，否则每道题都会尝试登录佳明
os.environ.setdefault("SYNC_PREFLIGHT_ENABLED", "0")

CASES = [
    ("q1_today", "我今天状态怎么样？"),
    ("q2_week", "这周跑了多少公里？"),
    ("q3_tonight", "今晚跑不跑？"),
    ("q4_hello", "你好"),
    ("q5_recovery", "我最近恢复得怎么样？"),
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--user", type=int, default=1001)
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--provider", default="", help="覆盖 LLM_PROVIDER（后端配置项）")
    p.add_argument("--out", default="")
    return p.parse_args()


async def run_all(user_id: int, repeat: int) -> list[dict]:
    from garmin_coach.agent import get_agent_pool

    pool = get_agent_pool()
    rows: list[dict] = []
    for round_no in range(1, repeat + 1):
        for case_id, question in CASES:
            # 评测隔离：内存 + 库 + 长期记忆一起清（见 SessionManager.reset）
            pool.reset(user_id, wipe_memory=True)
            t0 = time.perf_counter()
            try:
                result = await pool.get(user_id).collect(question)
                elapsed = int((time.perf_counter() - t0) * 1000)
                row = {
                    "case": case_id,
                    "round": round_no,
                    "question": question,
                    "turns": result.turns,
                    "tools": result.tools_used,
                    "tool_arg_list": [c.arguments for c in result.tool_calls],
                    "latency_ms": elapsed,
                    "llm_ms": result.llm_ms,
                    "tool_ms": result.tool_ms,
                    "usage": result.usage,
                    "reply_chars": len(result.text),
                    "truncated": result.truncated,
                    "error": result.error,
                    "steps": [
                        {"turn": s.turn, "kind": s.kind, "name": s.name,
                         "ms": s.latency_ms, "err": s.is_error}
                        for s in result.steps
                    ],
                    "reply_head": result.text[:80],
                    "ts": dt.datetime.now().isoformat(timespec="seconds"),
                }
            except Exception as exc:  # noqa: BLE001
                row = {
                    "case": case_id,
                    "round": round_no,
                    "question": question,
                    "error": f"{type(exc).__name__}: {exc}",
                    "latency_ms": int((time.perf_counter() - t0) * 1000),
                    "ts": dt.datetime.now().isoformat(timespec="seconds"),
                }
            rows.append(row)
            flag = "ERR" if row.get("error") else "OK "
            print(
                f"{flag} {case_id:12} turns={row.get('turns', '-')} "
                f"tools={','.join(row.get('tools') or []) or '-':40} "
                f"{row['latency_ms']:6d}ms llm={row.get('llm_ms', '-')}ms "
                f"tool={row.get('tool_ms', '-')}ms tok={row.get('usage', {})}"
            )
    return rows


def summarize(rows: list[dict]) -> dict:
    ok = [r for r in rows if not r.get("error")]
    if not ok:
        return {"ok": 0}
    turns = [r["turns"] for r in ok]
    lat = [r["latency_ms"] for r in ok]
    return {
        "ok": len(ok),
        "error": len(rows) - len(ok),
        "turns_avg": round(statistics.mean(turns), 2),
        "turns_max": max(turns),
        "latency_p50": int(statistics.median(lat)),
        "latency_max": max(lat),
        "llm_ms_avg": int(statistics.mean([r["llm_ms"] for r in ok])),
        "tool_ms_avg": int(statistics.mean([r["tool_ms"] for r in ok])),
    }


def main() -> None:
    args = parse_args()
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    if args.provider:
        # provider 现在由后端配置决定；命令行参数只作临时覆盖
        os.environ["LLM_PROVIDER"] = args.provider
    out = Path(args.out) if args.out else ROOT / "scripts" / "results" / f"smoke-{stamp}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)

    rows = asyncio.run(run_all(args.user, args.repeat))
    with out.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    summary = summarize(rows)
    print("\n汇总:", json.dumps(summary, ensure_ascii=False))
    print("原始结果:", out)


if __name__ == "__main__":
    main()
