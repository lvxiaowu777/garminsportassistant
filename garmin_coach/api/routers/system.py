"""系统级接口：首页、可用 LLM 列表。"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import FileResponse

from ..deps import STATIC_DIR

from garmin_coach.agent.llmclient.config import PROVIDERS

router = APIRouter(tags=["system"])


@router.get("/")
async def root():
    """Web 仪表盘入口。"""
    return FileResponse(STATIC_DIR / "index.html")


@router.get("/api/system/info")
async def api_info():
    """前端启动时读：哪些入口开放（注册/游客/文档）。"""
    from garmin_coach.settings import settings

    return {
        "register_enabled": settings.register_enabled,
        "demo_login_enabled": settings.demo_login_enabled,
        "demo_daily_messages": settings.demo_daily_messages,
        "docs_enabled": settings.docs_enabled,
    }


@router.get("/api/llm/providers")
async def api_providers():
    """列出可用的 LLM 提供商（数据源和 Agent 用的是同一份配置）。"""
    return {
        name: {
            "name": conf.display_name,
            "model": conf.model,
            "protocol": conf.protocol,
            "available": bool(conf.api_key),
        }
        for name, conf in PROVIDERS.items()
    }
