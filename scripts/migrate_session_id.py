"""迁移：给 chat_history 加 session_id 并回填历史数据（幂等）。

做三件事：
1. 加列 ``session_id VARCHAR(48) NULL``（已有则跳过）
2. 加索引 ``idx_chat_session (user_id, session_id, id)``（已有则跳过）
3. 回填老数据：``session_id = CONCAT('u', user_id, '-', LEFT(created_at, 10))``

用法（项目根目录）：
    python scripts/migrate_session_id.py            # 执行
    python scripts/migrate_session_id.py --dry-run  # 只看会改多少
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

INDEX_NAME = "idx_chat_session"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="只报告，不写库")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    from garmin_coach.db import database  # noqa: E402  （导入即加载 .env 里的 DATABASE_URL）
    from sqlalchemy import text  # noqa: E402

    engine = database.engine
    dialect = engine.dialect.name
    if dialect != "mysql":
        sys.exit(f"这个脚本只针对 MySQL，当前 DATABASE_URL 方言是 {dialect}")

    with engine.begin() as conn:
        # 1. 列
        col = conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name='chat_history' "
            "AND column_name='session_id'"
        )).scalar_one()
        if col:
            print("列 session_id 已存在，跳过")
        elif args.dry_run:
            print("[dry-run] 将新增列 session_id")
        else:
            conn.execute(text(
                "ALTER TABLE chat_history ADD COLUMN session_id VARCHAR(48) NULL "
                "AFTER user_id"
            ))
            print("已新增列 session_id")

        # 2. 索引
        idx = conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name='chat_history' "
            "AND index_name=:name"
        ), {"name": INDEX_NAME}).scalar_one()
        if idx:
            print(f"索引 {INDEX_NAME} 已存在，跳过")
        elif args.dry_run:
            print(f"[dry-run] 将新增索引 {INDEX_NAME}")
        else:
            conn.execute(text(
                f"CREATE INDEX {INDEX_NAME} ON chat_history (user_id, session_id, id)"
            ))
            print(f"已新增索引 {INDEX_NAME}")

        # 3. 回填
        todo = conn.execute(text(
            "SELECT COUNT(*) FROM chat_history WHERE session_id IS NULL"
        )).scalar_one()
        print(f"待回填行数：{todo}")
        if todo and not args.dry_run:
            conn.execute(text(
                "UPDATE chat_history "
                "SET session_id = CONCAT('u', user_id, '-', LEFT(created_at, 10)) "
                "WHERE session_id IS NULL"
            ))
            left = conn.execute(text(
                "SELECT COUNT(*) FROM chat_history WHERE session_id IS NULL"
            )).scalar_one()
            print(f"回填完成，剩余 NULL：{left}")

        # 4. 报告
        rows = conn.execute(text(
            "SELECT session_id, COUNT(*) c FROM chat_history "
            "GROUP BY session_id ORDER BY c DESC LIMIT 5"
        )).all()
        print("\n最大的几个会话：")
        for sid, count in rows:
            print(f"  {sid}: {count} 条")


if __name__ == "__main__":
    main()
