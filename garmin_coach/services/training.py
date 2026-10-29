"""训练记录的业务规则（手动记录只允许力量训练）。

数据层的 `record_training` 已经做了幂等；这里补的是**产品规则**：
跑步/骑行/游泳以手表为准，不接受手动记录。
"""
from __future__ import annotations

from garmin_coach.db.database import record_training
from garmin_coach.errors import AppError

MANUAL_TYPES = ("strength_training",)


def record_strength(
    user_id: int,
    train_type: str,
    duration_min: int,
    notes: str,
    date_str: str = "",
    exercises: list[dict] | None = None,
) -> dict:
    """记录一次力量训练，返回 {ok, created, added, merged, activity_id}。"""
    if train_type not in MANUAL_TYPES:
        raise AppError(
            f"{train_type} 以手表数据为准，不接受手动记录；只有力量训练可以手动记",
            400,
        )
    result = record_training(
        user_id, train_type, duration_min, notes, date_str, exercises or []
    )
    if not result.get("ok"):
        raise AppError(result.get("error") or "记录失败", 400)
    return result
