"""路由层共享依赖：鉴权、路径常量、品牌配置、logger。

**这个模块还有一个隐藏职责**：import `garmin_coach.db.legacy` 的副作用会把 `src/`
放进 `sys.path`，之后各路由里的 `from database import ...` 才成立。
这是绞杀者模式的过渡手段——`database.py` 整体搬进 `garmin_coach/db/` 之后，
这个副作用就可以删掉，届时路由改成 `from garmin_coach.db import ...`。
"""
from __future__ import annotations

import logging
from pathlib import Path

from garmin_coach.db import legacy as _legacy  # noqa: F401  副作用：把 src/ 放进 sys.path

from auth import get_current_user  # noqa: E402
from garmin_server_sync import sync_user_garmin  # noqa: E402

# 静态资源目录（项目根/static）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
STATIC_DIR = PROJECT_ROOT / "static"
AUDIO_DIR = STATIC_DIR / "audio"
STATIC_DIR.mkdir(exist_ok=True)
AUDIO_DIR.mkdir(exist_ok=True)

# 路由里用的 logger 与应用共用同一个名字，日志格式在 server.py 里统一配置
log = logging.getLogger("garmin.server")

# ── 手表品牌 ──
# 多品牌是设计的一部分，不是某个品牌专属的实现：所有数据表都有 brand 列，
# watch_bindings 按 (user_id, brand) 唯一，仪表盘按「当前启用品牌」取数。
# 加一个品牌只要两步：
#   1. 品牌名加进 SUPPORTED_BRANDS（不在这里的直接 400）
#   2. 有自动同步的，把同步函数加进 SYNCERS；只记录账号的不加（如 apple/huawei/xiaomi）
SUPPORTED_BRANDS = ("garmin", "apple", "huawei", "xiaomi")
SYNCERS = {"garmin": sync_user_garmin}

__all__ = [
    "AUDIO_DIR",
    "STATIC_DIR",
    "SUPPORTED_BRANDS",
    "SYNCERS",
    "get_current_user",
    "log",
]
