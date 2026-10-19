"""领域服务层 —— 本项目的业务规则，不依赖 FastAPI 也不依赖 LLM。

规划中的模块：

``safety.py``     安全档位引擎（从 ``src/safety_rules.py`` 迁）
                  纯函数：输入当天恢复数据，输出档位上限与降档原因，无 IO。
``sync.py``       佳明/高驰同步编排（从 ``*_server_sync.py`` 迁）
``weather.py``    天气拉取与跑步窗口计算（从 ``src/weather_server.py`` 迁）
``plans.py``      训练计划的生成与落库

为什么单独一层：这些逻辑既要被 HTTP 路由调用，也要被 LLM 工具调用。
放在 services/ 才能两边共享，避免「网页看到的和 AI 说的不一样」。
"""
