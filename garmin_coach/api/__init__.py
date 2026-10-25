"""HTTP 路由层 —— 只做「入参解析 → 调 service/agent → 序列化返回」。

这一层不写业务逻辑，也不直接碰 SQLAlchemy。结构（阶段 4 已完成，2026-10-24）：

    deps.py              鉴权透传 / 静态目录 / 品牌配置 / logger
                         （副作用：import 它会把 src/ 放进 sys.path）
    schemas.py           全部 Pydantic 请求模型
    routers/auth.py      /api/auth/*
    routers/watch.py     /api/watch/*
    routers/health.py    /api/health/*
    routers/chat.py      /api/chat/*  +  /api/asr  +  /ws/chat
    routers/user.py      /api/user/*
    routers/push.py      /api/push/*
    routers/system.py    / 、/api/llm/providers

拆分原则：路由函数体控制在十行以内，超过就说明逻辑该下沉到 services/ 或 agent/。
"""
