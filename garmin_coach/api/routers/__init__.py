"""HTTP 路由层：一个文件一个资源。

约定（也是判断有没有分层的标尺）：
- 路由函数**十行以内**，只做「解析入参 → 调 service/agent → 序列化返回」；
- 不写 SQL（数据库在 ``garmin_coach/db`` / ``src/database.py``）；
- 不写业务判断（规则在 ``garmin_coach/services``）；
- 请求/响应模型统一放 ``schemas.py``。

``deps.py`` 里 import 了 ``garmin_coach.db.legacy``，它的副作用是把 ``src/`` 放进
``sys.path``——所以每个路由模块第一件事是 ``from ..deps import ...``，之后才能
``from database import ...``。
"""
