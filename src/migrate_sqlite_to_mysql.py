"""
SQLite → MySQL 迁移脚本 —— 只迁指定账号（默认 13800138000）。
在服务器上跑：python src/migrate_sqlite_to_mysql.py
迁移前确认 MySQL 已装好、DATABASE_URL 已配置（脚本会先 init_db 建表）。
"""
import sys
import sqlite3
import argparse
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

SRC_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SRC_DIR))

from database import init_db, SessionLocal, DATABASE_URL
from models import User, WatchBinding, HealthDaily, Activity, ChatHistory

SQLITE_DB = SRC_DIR / "data" / "garmin_coach.db"


def _orm_cols(model):
    return [c.name for c in model.__table__.columns]


def migrate(phone: str):
    if not SQLITE_DB.exists():
        print(f"❌ 找不到 SQLite 库: {SQLITE_DB}")
        return

    sql_conn = sqlite3.connect(str(SQLITE_DB))
    sql_conn.row_factory = sqlite3.Row

    user_row = sql_conn.execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
    if not user_row:
        print(f"❌ 未找到账号 {phone}，现有账号：")
        for r in sql_conn.execute("SELECT id, phone FROM users"):
            print(f"   id={r['id']}  phone={r['phone']}")
        return

    uid = user_row["id"]
    print(f"目标账号: {phone} (user_id={uid})")
    print(f"MySQL: {DATABASE_URL.split('@')[-1]}")

    init_db()  # 建表（幂等）

    session = SessionLocal()
    try:
        # users
        u_cols = _orm_cols(User)
        session.add(User(**{c: user_row[c] for c in u_cols if c in user_row.keys()}))
        print("  ✅ users: 1 条")

        # watch_bindings
        wb_rows = sql_conn.execute(
            "SELECT * FROM watch_bindings WHERE user_id=?", (uid,)
        ).fetchall()
        wb_cols = _orm_cols(WatchBinding)
        for r in wb_rows:
            session.add(WatchBinding(**{c: r[c] for c in wb_cols if c in r.keys()}))
        print(f"  ✅ watch_bindings: {len(wb_rows)} 条")

        # health_daily（清洗脏数据：location/now_* 后加的列在原 SQLite 里字段错位存了时间戳）
        hd_rows = sql_conn.execute(
            "SELECT * FROM health_daily WHERE user_id=?", (uid,)
        ).fetchall()
        hd_cols = _orm_cols(HealthDaily)
        _dirty_cols = {"location", "now_temp", "now_feels_like", "now_text", "now_precip"}
        for r in hd_rows:
            kwargs = {}
            for c in hd_cols:
                if c not in r.keys():
                    continue
                kwargs[c] = None if c in _dirty_cols else r[c]
            session.add(HealthDaily(**kwargs))
        print(f"  ✅ health_daily: {len(hd_rows)} 条")

        # activities
        act_rows = sql_conn.execute(
            "SELECT * FROM activities WHERE user_id=?", (uid,)
        ).fetchall()
        act_cols = _orm_cols(Activity)
        for r in act_rows:
            session.add(Activity(**{c: r[c] for c in act_cols if c in r.keys()}))
        print(f"  ✅ activities: {len(act_rows)} 条")

        # chat_history
        ch_rows = sql_conn.execute(
            "SELECT * FROM chat_history WHERE user_id=?", (uid,)
        ).fetchall()
        ch_cols = _orm_cols(ChatHistory)
        for r in ch_rows:
            session.add(ChatHistory(**{c: r[c] for c in ch_cols if c in r.keys()}))
        print(f"  ✅ chat_history: {len(ch_rows)} 条")

        session.commit()
        print("\n✅ 迁移完成（device_tokens 未迁，推送未启用）")
    except Exception as e:
        session.rollback()
        print(f"\n❌ 迁移失败: {e}")
        raise
    finally:
        session.close()
        sql_conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="只迁移指定手机号账号的数据")
    parser.add_argument("--phone", default="13800138000", help="手机号（默认 13800138000）")
    args = parser.parse_args()
    migrate(args.phone)
