"""对话前的新鲜度保障。

产品约定：**跑步数据只信手表（佳明）**，用户问"今天跑得怎么样"时，
先同步一次再回答，而不是拿几小时前的快照糊过去。

策略：
- 上次同步在 ``SYNC_FRESH_TTL_MINUTES``（默认 10 分钟）内 → 直接用，不打扰手表；
- 超过 TTL → 同步最近 1 天（只拉今天，不是 7 天）；
- 超时（默认 25s）/ 未绑定 / token 过期 → **不阻塞对话**，继续用旧数据，
  同时把"数据截至"告诉模型（注入块里带时间）。
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

from garmin_coach.db.database import get_last_sync_at
from garmin_coach.integrations.garmin_sync import sync_user_garmin
from garmin_coach.settings import settings

log = logging.getLogger(__name__)


def _parse(ts: str) -> dt.datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(ts.strip(), fmt)
        except (ValueError, AttributeError):
            continue
    return None


def is_fresh(last_sync_at: str, ttl_minutes: int) -> bool:
    """上次同步时间是否还在 TTL 内。"""
    parsed = _parse(last_sync_at or "")
    if parsed is None:
        return False
    return (dt.datetime.now() - parsed) < dt.timedelta(minutes=ttl_minutes)


async def ensure_fresh(
    user_id: int,
    *,
    ttl_minutes: int | None = None,
    timeout_seconds: int | None = None,
) -> dict:
    """必要时同步一次，返回 {synced, reason, last_sync_at, error}。

    永远不抛异常 —— 同步失败不应该让对话失败。
    """
    ttl = ttl_minutes if ttl_minutes is not None else settings.sync_fresh_ttl_minutes
    timeout = (
        timeout_seconds if timeout_seconds is not None
        else settings.sync_fresh_timeout_seconds
    )

    if not settings.sync_preflight_enabled:
        return {"synced": False, "reason": "disabled", "last_sync_at": "", "error": None}

    try:
        last = await asyncio.to_thread(get_last_sync_at, user_id)
    except Exception:  # noqa: BLE001
        last = ""

    if is_fresh(last, ttl):
        return {"synced": False, "reason": "fresh", "last_sync_at": last, "error": None}

    try:
        days, err = await asyncio.wait_for(
            asyncio.to_thread(sync_user_garmin, user_id, 1),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        log.warning("对话前同步超时(%ss)，先用旧数据 user=%s", timeout, user_id)
        return {"synced": False, "reason": "timeout", "last_sync_at": last, "error": None}
    except Exception as exc:  # noqa: BLE001
        log.warning("对话前同步失败 user=%s: %s", user_id, exc)
        return {"synced": False, "reason": "error", "last_sync_at": last, "error": str(exc)}

    try:
        last = await asyncio.to_thread(get_last_sync_at, user_id)
    except Exception:  # noqa: BLE001
        pass

    log.info("对话前同步完成 user=%s days=%s err=%s", user_id, days, err)
    return {
        "synced": bool(days),
        "reason": "synced" if days else "failed",
        "last_sync_at": last,
        "error": err,
    }


def data_note(fresh: dict) -> str:
    """把数据新鲜度拼成一行给模型看的提示（注入块用）。"""
    ts = (fresh.get("last_sync_at") or "").strip()
    if not ts:
        return "[数据] 手表数据尚未同步；如果用户问跑步/心率等数据，先说明可能不完整。"
    stamp = ts[5:16] if len(ts) >= 16 else ts
    if fresh.get("synced"):
        return f"[数据] 刚刚同步过手表数据（{stamp}）。跑步/骑行数据以手表为准。"
    return f"[数据] 手表数据截至 {stamp}。跑步/骑行数据以手表为准。"
