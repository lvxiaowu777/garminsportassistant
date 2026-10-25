"""Garmin Coach —— 应用入口（薄壳）。

这个文件只做四件事：
1. 把 `src/` 和项目根放进 `sys.path`（用 `python src/server.py` 启动时两个都要）；
2. 创建 FastAPI 应用、配置日志；
3. 把各资源的路由挂上来，最后挂静态目录；
4. 直接运行时启动定时任务 + uvicorn。

**业务代码一律不在这个文件里**：
- 路由 → `garmin_coach/api/routers/*.py`
- 请求/响应模型 → `garmin_coach/api/schemas.py`
- 共享依赖（鉴权/路径/品牌配置）→ `garmin_coach/api/deps.py`
- 领域规则 → `garmin_coach/services/`（安全档位、训练阶段、对话上下文、标记解析）
- 数据访问 → `src/database.py`（后续搬进 `garmin_coach/db/`）
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# src/：`python src/server.py` 时 sys.path[0] 已经是 src/，但被其他方式导入时要补齐
SRC_DIR = Path(__file__).resolve().parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# 项目根：新架构包 garmin_coach/ 在这里，不加上 `import garmin_coach` 会 ModuleNotFoundError
PROJECT_ROOT = SRC_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from garmin_coach.api.deps import STATIC_DIR  # noqa: E402
from garmin_coach.api.routers import (  # noqa: E402
    auth,
    chat,
    health,
    push,
    system,
    user,
    watch,
)

app = FastAPI(title="Garmin Coach", version="1.0.0")

# 让 Agent 的工具调用过程能在服务器日志里看到（agent / garmin.server 两个 logger）。
# 只在没人配过 handler 时设一次，避免覆盖 uvicorn 自己的配置。
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

# 路由：一个资源一个模块
for _router in (
    auth.router,
    watch.router,
    health.router,
    chat.router,
    user.router,
    push.router,
    system.router,
):
    app.include_router(_router)

# 静态文件（含 /static/audio 语音，必须在路由之后挂）
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    from apscheduler.schedulers.background import BackgroundScheduler

    from database import cleanup_old_chat, init_db
    from garmin_coach.agent.session import get_session_manager
    from garmin_coach.settings import settings
    from garmin_server_sync import sync_all_users
    from push_service import check_and_notify, init_firebase

    init_db()
    init_firebase()
    print("✅ 数据库就绪")

    scheduler = BackgroundScheduler()
    # 定时推送检查：每天 8:07 和 19:07
    scheduler.add_job(check_and_notify, "cron", hour=8, minute=7, id="push_morning")
    scheduler.add_job(check_and_notify, "cron", hour=19, minute=7, id="push_evening")
    # 佳明同步：每天 2:07，拉最近 7 天（多品牌见 api/deps.py 的 SYNCERS）
    scheduler.add_job(sync_all_users, "cron", hour=2, minute=7, id="garmin_sync_daily", kwargs={"days": 7})
    scheduler.add_job(cleanup_old_chat, "cron", hour=3, minute=7, id="chat_cleanup", kwargs={"days": 7})
    # 会话回收：按 settings.session_reap_interval_seconds 扫描空闲会话
    scheduler.add_job(
        get_session_manager().reap,
        "interval",
        seconds=settings.session_reap_interval_seconds,
        id="session_reap",
    )
    scheduler.start()
    print(
        "🔔 定时任务已启动（同步 2:07 / 清理 3:07 / "
        f"会话回收 {settings.session_reap_interval_seconds}s / 推送 8:07、19:07）"
    )

    print("🚀 启动 Garmin Coach 服务: http://localhost:8766")
    uvicorn.run(app, host="0.0.0.0", port=8766, log_level="info")
