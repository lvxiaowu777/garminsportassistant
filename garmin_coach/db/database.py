"""数据访问层的**门面**（历史入口，保持不变）。

真正的实现按表拆在 ``garmin_coach/db/repositories/``：

```
_base.py       引擎 / Session / 建表补列 / 写入辅助
users.py       users：注册、档案、同步时间戳
watches.py     watch_bindings：绑定、开关、当前品牌
activities.py  activities + strength_sets：活动、手动力量记录（幂等）
health.py      health_daily：单日/区间/仪表盘
chat.py        chat_history：聊天读写与清理
plans.py       daily_plans：训练计划
push.py        device_tokens：推送 token
```

``from garmin_coach.db.database import xxx`` 的老写法不用改。
"""
from __future__ import annotations

from .repositories import *  # noqa: F401,F403
from .repositories import __all__  # noqa: F401
