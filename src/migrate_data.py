"""
数据迁移：从现有 markdown 文件导入到 SQLite。
只读不写，不影响现有数据。
"""
import os
import sys
import datetime as dt
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from frontmatter import parse as parse_health_md
from database import init_db, upsert_health_daily

HEALTH_DIR = (
    Path(os.environ.get("GARMIN_STUDY_DIR") or (Path.home() / "study"))
    / "知识库"
    / "健康"
)


def migrate_all():
    init_db()
    count = 0
    for md_file in sorted(HEALTH_DIR.rglob("*.md")):
        try:
            data = parse_health_md(md_file)
            if not data or not data.get("date"):
                continue
            upsert_health_daily(data)
            count += 1
            print(f"  ✅ {data['date']}")
        except Exception as e:
            print(f"  ⚠️ {md_file}: {e}")

    print(f"\n✅ 迁移完成，共 {count} 天")


if __name__ == "__main__":
    migrate_all()