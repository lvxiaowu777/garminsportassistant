"""时段判断：把小时折成「深夜 / 早晨 / 上午 / 下午 / 傍晚 / 夜晚」。

dashboard 和旧对话上下文都要用，抽出来避免两处规则漂移。
"""
from __future__ import annotations


def period_of(hour: int) -> str:
    if hour < 5:
        return "深夜"
    if hour < 10:
        return "早晨"
    if hour < 14:
        return "上午"
    if hour < 18:
        return "下午"
    if hour < 22:
        return "傍晚"
    return "夜晚"
