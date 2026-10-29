"""演示站配额：进程内计数（单 worker 足够；多 worker 需换 Redis）。

三类限制：
- 每个访客每分钟 N 条；
- 每个访客每天 M 条；
- 全站每天 K 条（防"换 IP 轮着刷"）。
"""
from __future__ import annotations

import datetime as dt
import threading

from garmin_coach.errors import AppError
from garmin_coach.settings import settings

_lock = threading.Lock()
_buckets: dict[str, int] = {}


def _bump(key: str) -> int:
    with _lock:
        value = _buckets.get(key, 0) + 1
        _buckets[key] = value
        return value


def _get(key: str) -> int:
    with _lock:
        return _buckets.get(key, 0)


def _cleanup(now: dt.datetime) -> None:
    """清掉昨天以前的键，避免字典无限增长。"""
    keep = {now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d %H:%M")}
    with _lock:
        for key in list(_buckets):
            tail = key.rsplit(":", 1)[-1]
            if len(tail) == 16:      # 分钟桶
                if tail != now.strftime("%Y-%m-%d %H:%M"):
                    _buckets.pop(key, None)
            elif len(tail) == 10 and tail not in keep:
                _buckets.pop(key, None)


def check_demo_chat(client_ip: str) -> None:
    """游客体验的配额检查；超限抛 429。"""
    now = dt.datetime.now()
    _cleanup(now)
    minute_key = f"chat:m:{client_ip}:{now.strftime('%Y-%m-%d %H:%M')}"
    day_key = f"chat:d:{client_ip}:{now.strftime('%Y-%m-%d')}"
    global_key = f"chat:g:{now.strftime('%Y-%m-%d')}"

    if _get(minute_key) >= settings.demo_per_minute:
        raise AppError("演示账号每分钟有次数限制，缓一下再发～", 429)
    if _get(day_key) >= settings.demo_daily_messages:
        raise AppError("今天的演示额度用完了，明天再来～", 429)
    if _get(global_key) >= settings.demo_global_daily:
        raise AppError("全站演示额度已用完，明天再来～", 429)

    _bump(minute_key)
    _bump(day_key)
    _bump(global_key)


def check_message(message: str) -> None:
    """单条消息长度限制（防 prompt stuffing 烧 token）。"""
    if len(message) > settings.max_message_chars:
        raise AppError(f"单条消息最多 {settings.max_message_chars} 字", 400)
