"""HTTP 路由层 —— 只做「入参解析 → 调 service/agent → 序列化返回」。

这一层不写业务逻辑，也不直接碰 SQLAlchemy。

阶段 4 把 ``src/server.py`` 里约 40 个路由按资源拆到本包：

    auth.py     /api/auth/*
    watch.py    /api/watch/*
    health.py   /api/health/*
    chat.py     /api/chat/*  +  /ws/chat
    push.py     /api/push/*
    asr.py      /api/asr

拆分原则：路由函数体控制在十行以内，超过就说明逻辑该下沉到 services/ 或 agent/。
"""
