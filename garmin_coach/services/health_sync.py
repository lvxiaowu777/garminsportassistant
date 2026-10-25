"""按品牌分发同步（手动同步 / 定时同步的业务规则）。

规则：
- 只有**启用中**的绑定才同步；
- 当天第一次同步拉 7 天（补历史），之后只拉当天；
- 没有实现同步器的品牌（apple/huawei/xiaomi）返回 note，不算失败；
- 全部品牌都失败时抛 ``AppError``（路由统一转 4xx/5xx）。
"""
from __future__ import annotations

import datetime as dt
import logging

from garmin_coach.db.database import (
    get_last_sync_date,
    get_watch_bindings,
    update_last_sync_date,
)
from garmin_coach.errors import AppError
from garmin_coach.integrations.garmin_sync import sync_user_garmin

log = logging.getLogger(__name__)

#: 品牌 → 同步函数。加品牌时在这里加一行（只记录账号的品牌不加）
SYNCERS = {"garmin": sync_user_garmin}


def sync_all_brands(user_id: int) -> dict:
    """同步该用户所有启用的设备，返回 {results, total_days}。"""
    today = dt.date.today().isoformat()
    days = 1 if get_last_sync_date(user_id) == today else 7

    results: dict[str, dict] = {}
    brands = [b["brand"] for b in get_watch_bindings(user_id) if b.get("enabled")]
    for brand in brands:
        sync_fn = SYNCERS.get(brand)
        if sync_fn is None:
            results[brand] = {"days": 0, "error": None, "note": "该品牌暂不支持自动同步"}
            continue
        n, err = sync_fn(user_id, days=days)
        results[brand] = {"days": n, "error": err}

    total = sum(v["days"] for v in results.values())
    if total:
        update_last_sync_date(user_id, today)

    errors = [v["error"] for v in results.values() if v["error"]]
    log.info("sync user=%s days=%s total=%s errors=%s", user_id, days, total, errors)
    if errors and total == 0:
        raise AppError("同步失败: " + "；".join(errors), 502)

    return {"results": results, "total_days": total, "days": days}
