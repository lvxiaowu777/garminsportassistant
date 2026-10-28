"""路由层共享依赖：鉴权、路径常量、品牌配置、logger。

分层约定（避免路由里又出现业务判断）：

- 路由只做：**鉴权 + 参数 + 调 service + 返回**；
- 需要登录的接口用 ``Depends(require_user)``，不要在函数体里写 `if user_id == 0`；
- 业务规则抛 ``AppError``（`garmin_coach/errors.py`），由 `server.py` 统一翻译成状态码。
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import Depends

from garmin_coach.auth import get_current_user
from garmin_coach.errors import AuthRequiredError

# 静态资源目录（项目根/static）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
STATIC_DIR = PROJECT_ROOT / "static"
AUDIO_DIR = STATIC_DIR / "audio"
STATIC_DIR.mkdir(exist_ok=True)
AUDIO_DIR.mkdir(exist_ok=True)

# 路由里用的 logger 与应用共用同一个名字，日志格式在 server.py 里统一配置
log = logging.getLogger("garmin.server")

__all__ = [
    "AUDIO_DIR",
    "STATIC_DIR",
    "SUPPORTED_BRANDS",
    "SYNCERS",
    "get_current_user",
    "log",
    "require_user",
]


async def require_user(user_id: int = Depends(get_current_user)) -> int:
    """需要登录的接口统一用它：未登录直接 401，路由里不再重复判断。"""
    if not user_id:
        raise AuthRequiredError()
    return user_id
