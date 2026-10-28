"""重建数据库（开发期改表结构后用它，不保留数据）。

用法：
    python scripts/reset_db.py --yes
    python scripts/seed_dev_data.py --password 123456 --reset
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from garmin_coach.db import database as db


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--yes", action="store_true", help="确认删除所有表")
    args = parser.parse_args()
    if not args.yes:
        print("这会删掉所有表。确认请加 --yes")
        raise SystemExit(1)

    db.reset_db()
    print("✅ 表已重建（结构与 models.py 一致）")
    print("下一步：python scripts/seed_dev_data.py --password 123456 --reset")


if __name__ == "__main__":
    main()
