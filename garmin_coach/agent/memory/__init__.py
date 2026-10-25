"""长期记忆模块。

```
store.py     user_memory 表读写（upsert / top / 整表替换）
inject.py    序列化成 [记忆] 块（带"多久前更新"、置信度）
extract.py   提炼：一次 LLM 调用；失败退规则版
manage.py    管理 Agent：合并重复、冲突取新、过期删除
outbox.py    会话结束投递 + 消费（提炼 → 写入 → 治理）
```

读路径（`inject`）在对话前同步执行；写路径（`outbox` → `extract` → `manage`）
离线执行，不占请求时间。
"""
from .inject import build_injected, format_memory_block, humanize_age
from .outbox import enqueue_session, process_outbox
from .store import all_memories, replace_memories, top_memories, upsert_memory

__all__ = [
    "all_memories",
    "build_injected",
    "enqueue_session",
    "format_memory_block",
    "humanize_age",
    "process_outbox",
    "replace_memories",
    "top_memories",
    "upsert_memory",
]
