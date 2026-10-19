"""Garmin Coach —— 新架构包（阶段 1 起逐步迁入）。

重构采用**绞杀者模式**（strangler pattern）：
旧实现继续留在 ``src/`` 里跑，新代码写在这个包里，一个模块一个模块地把职责搬过来，
每搬完一个就在旧文件里留一行转发，直到 ``src/`` 可以整体删除。

分层与 MewCode 的对应关系：

======================  ======================  ==========================
本包模块                职责                    MewCode 对应
======================  ======================  ==========================
``garmin_coach/api/``   HTTP 路由（薄壳）        无（FastAPI 特有）
``garmin_coach/agent/`` Agent 主循环            ``mewcode/agent.py``
``garmin_coach/llm/``   LLM 传输与流式解析        ``mewcode/client.py``
``garmin_coach/tools/`` 工具注册表与工具实现      ``mewcode/tools/``
``garmin_coach/services/`` 领域规则（安全档位等）  无（本项目自有逻辑）
``garmin_coach/db/``    数据访问（SQLAlchemy）    无
======================  ======================  ==========================

阶段 0 只建立骨架，各包内除 ``__init__.py`` 外没有实现。
"""
