"""数据访问层。

```
database.py   engine / SessionLocal + 全部查询与写入函数（原 src/database.py）
models.py     ORM 模型（9 张业务表 + user_memory / memory_outbox / agent_runs / agent_steps）
legacy.py     过渡层：把 src/ 放进 sys.path（auth / 同步 / 推送还在 src）
```

**注意**：这个包不在 import 时建 engine —— ``database`` 模块需要 ``DATABASE_URL``
环境变量，导入即校验。所以这里不 re-export，用哪个模块直接 import：

```python
from garmin_coach.db import database
from garmin_coach.db.models import ChatHistory
```

下一步（未做）：把 ``database.py`` 按聚合根拆成 ``repositories/``
（user / health / activity / chat），现在它仍是单文件 ~1200 行。
"""
