"""工具层 —— 对应 MewCode 的 ``mewcode/tools/``。

规划中的模块：

``base.py``
    ``Tool`` 抽象基类与 ``ToolResult``。
    每个工具需要：``name``（模型看到的名字）、``description``（给模型读的说明书）、
    ``params_model``（pydantic 校验入参）、``async execute()``。

``registry.py``
    ``ToolRegistry``：按名字注册/查找工具，并导出成 LLM 需要的 JSON Schema。

``health.py`` / ``safety.py`` / ``records.py``
    具体工具实现，见下。

计划中的工具清单（对应现有 HTTP 接口的能力）：

    get_safety()              当前安全档位与降档原因    ← /api/health/safety
    get_today()               今日恢复、睡眠、天气      ← /api/health/dashboard
    get_recent(days)          最近 N 天聚合             ← /api/health/recent/{days}
    get_day(date)             指定日期详情             ← /api/health/day/{date}
    get_activity(activity_id) 单次活动细节（分段/心率区间）
    get_weather()             今日天气与跑步窗口        ← /api/health/weather
    get_profile()             训练目标与未来比赛
    record_training(...)      记一条训练               ← /api/health/record-training

注意：工具**复用 services/ 里的函数**，不自己写查询逻辑。
这样 HTTP 路由和 LLM 工具共享同一份业务实现，不会出现两套口径。
"""
