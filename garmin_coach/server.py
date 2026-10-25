"""应用组装与启动（包内的 server 模块）。

`src/server.py` 只是启动器（负责 sys.path 与调用 `main()`），
真正的 app 在这里组装：

1. 建 FastAPI 应用、配日志；
2. 按资源挂路由，最后挂静态目录；
3. `main()` 里：建表 → 起定时任务 → uvicorn。

**业务代码一律不在这个文件里**：
- 路由 → `garmin_coach/api/routers/*.py`
- 请求模型 → `garmin_coach/api/schemas.py`
- 共享依赖 → `garmin_coach/api/deps.py`
- 领域规则 → `garmin_coach/services/`
- 外部服务 → `garmin_coach/integrations/`
- 数据访问 → `garmin_coach/db/`
"""
from __future__ import annotations

import logging
import sys

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .api.deps import STATIC_DIR
from .errors import AppError
from .api.routers import auth, chat, health, push, system, user, watch

# Windows 控制台默认 GBK，中文日志会炸；只影响本地开发
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

app = FastAPI(title="Garmin Coach", version="1.0.0")


@app.exception_handler(AppError)
async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """业务异常 → HTTP 状态码（service 层只管抛，路由不用写 try/except）。"""
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

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


def start_scheduler() -> None:
    """定时任务：同步 / 清理 / 会话回收 / 推送。"""
    from apscheduler.schedulers.background import BackgroundScheduler

    from .agent.session import get_session_manager
    from .db.database import cleanup_old_chat
    from .integrations.garmin_sync import sync_all_users
    from .integrations.push import check_and_notify
    from .settings import settings

    scheduler = BackgroundScheduler()
    # 定时推送检查：每天 8:07 和 19:07
    scheduler.add_job(check_and_notify, "cron", hour=8, minute=7, id="push_morning")
    scheduler.add_job(check_and_notify, "cron", hour=19, minute=7, id="push_evening")
    # 佳明同步：每天 2:07，拉最近 7 天（多品牌见 api/deps.py 的 SYNCERS）
    scheduler.add_job(
        sync_all_users, "cron", hour=2, minute=7,
        id="garmin_sync_daily", kwargs={"days": 7},
    )
    # 聊天记录清理：每天 3:07
    scheduler.add_job(
        cleanup_old_chat, "cron", hour=3, minute=7,
        id="chat_cleanup", kwargs={"days": 7},
    )
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


def main() -> None:
    """启动服务（`python src/server.py` 和 `python -m garmin_coach.server` 都走这里）。"""
    import uvicorn

    from .db.database import init_db
    from .integrations.push import init_firebase

    init_db()
    init_firebase()
    print("✅ 数据库就绪")

    start_scheduler()

    print("🚀 启动 Garmin Coach 服务: http://localhost:8766")
    uvicorn.run(app, host="0.0.0.0", port=8766, log_level="info")


if __name__ == "__main__":
    main()
