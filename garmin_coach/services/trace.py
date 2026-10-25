"""运行轨迹（trace）：把一次 run 的每一步落库。

为什么要落库而不是只打日志：
- **评测**要从这里算轮次、工具序列、耗时、失败率，不能靠正则解析日志；
- **压测**要能回答"是 LLM 慢还是工具慢"；
- **演示**要把一次真实对话的过程截图出来。

一条 run = 一轮用户消息（可能包含多次 LLM 调用和工具调用）。
"""
from __future__ import annotations

import datetime as dt
import json

from garmin_coach.db.database import SessionLocal
from garmin_coach.db.models import AgentRun, AgentStep
from sqlalchemy import func, select

RESULT_PREVIEW = 500  # 工具结果只存前 N 字符，避免 trace 表膨胀


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def save_run(
    *,
    user_id: int,
    session_id: str,
    provider: str,
    turns: int,
    total_tokens: int,
    latency_ms: int,
    error: str | None,
    steps: list,
    tool_calls: list,
) -> int | None:
    """写一条 run + 它的所有 steps。返回 run_id；失败返回 None（不抛）。"""
    session = SessionLocal()
    try:
        run = AgentRun(
            user_id=user_id,
            session_id=session_id,
            provider=provider,
            turns=turns,
            total_tokens=total_tokens,
            latency_ms=latency_ms,
            error=error,
        )
        session.add(run)
        session.flush()  # 拿到 run.id

        # 工具步骤要补上入参/结果：按 (name) 顺序对应 ToolCallRecord
        by_name: dict[str, list] = {}
        for call in tool_calls:
            by_name.setdefault(call.name, []).append(call)

        for step in steps:
            args_json = None
            result_text = None
            if step.kind == "tool":
                bucket = by_name.get(step.name) or []
                call = bucket.pop(0) if bucket else None
                if call is not None:
                    try:
                        args_json = json.dumps(call.arguments, ensure_ascii=False)
                    except (TypeError, ValueError):
                        args_json = None
                    result_text = (call.result or "")[:RESULT_PREVIEW]

            session.add(AgentStep(
                run_id=run.id,
                turn=step.turn,
                kind=step.kind,
                name=step.name,
                latency_ms=step.latency_ms,
                is_error=int(bool(step.is_error)),
                chars=step.chars,
                args_json=args_json,
                result_text=result_text,
            ))

        session.commit()
        return run.id
    except Exception:
        session.rollback()
        return None
    finally:
        session.close()


def recent_runs(user_id: int, limit: int = 20) -> list[dict]:
    """最近 N 次运行（前端/评测用）。"""
    session = SessionLocal()
    try:
        rows = session.execute(
            select(AgentRun)
            .where(AgentRun.user_id == user_id)
            .order_by(AgentRun.id.desc())
            .limit(limit)
        ).scalars().all()
        return [
            {
                "id": r.id,
                "session_id": r.session_id,
                "provider": r.provider,
                "turns": r.turns,
                "total_tokens": r.total_tokens,
                "latency_ms": r.latency_ms,
                "error": r.error,
                "created_at": r.created_at,
            }
            for r in rows
        ]
    finally:
        session.close()


def run_steps(run_id: int) -> list[dict]:
    """一次运行的全部步骤（按顺序）。"""
    session = SessionLocal()
    try:
        rows = session.execute(
            select(AgentStep).where(AgentStep.run_id == run_id).order_by(AgentStep.id)
        ).scalars().all()
        return [
            {
                "turn": s.turn,
                "kind": s.kind,
                "name": s.name,
                "latency_ms": s.latency_ms,
                "is_error": bool(s.is_error),
                "chars": s.chars,
                "args_json": s.args_json,
                "result_text": s.result_text,
            }
            for s in rows
        ]
    finally:
        session.close()


def stats(user_id: int, days: int = 7) -> dict:
    """最近 N 天的聚合指标（评测 summary 的数据源）。"""
    session = SessionLocal()
    try:
        cutoff = (dt.datetime.now() - dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        base = select(AgentRun).where(
            AgentRun.user_id == user_id, AgentRun.created_at >= cutoff
        )
        rows = session.execute(base).scalars().all()
        if not rows:
            return {"runs": 0}

        lat = sorted(r.latency_ms for r in rows)
        n = len(lat)
        p50 = lat[n // 2]
        p95 = lat[min(n - 1, int(n * 0.95))]
        turns = [r.turns for r in rows]

        tool_rows = session.execute(
            select(AgentStep)
            .where(AgentStep.kind == "tool")
            .where(AgentStep.run_id.in_([r.id for r in rows]))
        ).scalars().all()

        return {
            "runs": n,
            "turns_avg": round(sum(turns) / n, 2),
            "turns_max": max(turns),
            "latency_p50": p50,
            "latency_p95": p95,
            "latency_max": max(lat),
            "errors": sum(1 for r in rows if r.error),
            "tokens_avg": int(sum(r.total_tokens for r in rows) / n),
            "tools_total": len(tool_rows),
            "tool_ms_avg": int(sum(t.latency_ms for t in tool_rows) / len(tool_rows)) if tool_rows else 0,
            "tool_errors": sum(1 for t in tool_rows if t.is_error),
        }
    finally:
        session.close()
