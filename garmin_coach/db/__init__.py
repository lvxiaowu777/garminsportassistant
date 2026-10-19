"""数据访问层 —— SQLAlchemy 模型与仓储函数。

规划中的模块：

``models.py``
    ORM 模型（从 ``src/models.py`` 迁）：User / WatchBinding / HealthDaily /
    Activity / ChatHistory / DeviceToken / ExerciseName / StrengthSet / DailyPlan

``session.py``
    engine 与 SessionLocal 的创建。
    **关键改动**：连接串必须来自 ``DATABASE_URL`` 环境变量，
    不再像旧 ``src/database.py`` 那样把 MySQL 账号密码写成默认值。

``repositories.py``
    按聚合根拆分的查询函数，替代 ``src/database.py``（44KB，约 1200 行）
    那种「所有表的所有查询塞在一个文件」的组织方式。
    建议按 User / Health / Activity / Chat 拆成四个仓储模块。

迁移要点：旧 ``database.py`` 顶部还带一段手写的 .env 解析和硬编码 Windows
绝对路径（``C:\\Users\\...\\study\\.env``），这部分在迁移时删掉，
改用标准的 dotenv 加载。
"""
