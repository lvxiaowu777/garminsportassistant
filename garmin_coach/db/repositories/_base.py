"""引擎 / Session / 建表与补列 / 写入辅助（MySQL only）。"""
"""
数据访问层 —— SQLAlchemy + MySQL（健康数据、活动、聊天、记忆、trace）。
对外函数签名保持稳定，内部用 SQLAlchemy Session。
"""
import sys
import os
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


# 加载 .env：本文件在 garmin_coach/db/ 下，项目根是上三层
ENV_PATH = Path(__file__).resolve().parent.parent.parent.parent / ".env"
if not ENV_PATH.exists():
    # 兼容旧的本地布局：<用户主目录>/study/.env
    # 不硬编码用户名，换成 Path.home() 推导；可用 GARMIN_STUDY_DIR 覆盖。
    ENV_PATH = (
        Path(os.environ.get("GARMIN_STUDY_DIR") or (Path.home() / "study")) / ".env"
    )
if ENV_PATH.exists():
    # 和 settings.py 用同一套规则：python-dotenv，override=False
    # （真实环境变量优先于 .env，容器部署时不会被文件覆盖）
    try:
        from dotenv import load_dotenv

        load_dotenv(ENV_PATH, override=False)
    except ImportError:  # pragma: no cover - 没装 dotenv 时退回极简解析
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    os.environ.setdefault(key.strip(), val.strip())

from sqlalchemy import create_engine, func, inspect, text
from sqlalchemy.dialects.mysql import insert as _mysql_insert
from sqlalchemy.orm import sessionmaker

from garmin_coach.db.models import Base

# 数据库连接串只从环境变量读。历史版本在这里硬编码了 MySQL 账号密码，
# 阶段 0库 重构时移除 —— 连接串属于环境配置，不该写进代码，更不该进版本。
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    raise RuntimeError(
        "未设置 DATABASE_URL 环境变量。\n"
        "  mysql+pymysql://<user>:<pass>@localhost:3306/garmin_coach?charset=utf8mb4\n"
        "写进项目根目录的 .env 即可（该文件已 gitignore）。"
    )

engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=3600, echo=False)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


# ═══════════════════════════════════════════════
# 写入辅助（MySQL 专用）
# ═══════════════════════════════════════════════
#
# 只支持 MySQL：SQLite 已废弃（双方言分派是维护负担，且本地也能起 MySQL）。
# 数据库方言由 `engine.dialect.name` 之外的地方不再做兼容。




def _insert_stmt(model, values: dict):
    """构造 INSERT（尚未附加冲突处理）。"""
    return _mysql_insert(model).values(**values)


def _upsert(
    model,
    values: dict,
    conflict_cols: list[str],
    update_keys: list[str] | None = None,
    do_nothing: bool = False,
):
    """「插入或更新」（MySQL ``ON DUPLICATE KEY UPDATE``）。

    conflict_cols : 冲突判定键。MySQL 靠 PRIMARY/UNIQUE 自动识别，这里只用于
                    推导"除冲突键外要覆盖哪些列"。
    update_keys   : 冲突时要覆盖的列；None 表示「除冲突键外全部」。
    do_nothing    : True 表示冲突时保持原值（等价于 INSERT IGNORE）。
    """
    stmt = _insert_stmt(model, values)

    if do_nothing:
        col = conflict_cols[0]
        return stmt.on_duplicate_key_update(**{col: getattr(model, col)})

    keys = (
        update_keys
        if update_keys is not None
        else [k for k in values if k not in conflict_cols]
    )
    if not keys:
        return _upsert(model, values, conflict_cols, do_nothing=True)

    return stmt.on_duplicate_key_update(**{k: stmt.inserted[k] for k in keys})


def _upsert_append(
    model,
    values: dict,
    conflict_cols: list[str],
    col_name: str,
    suffix: str,
):
    """upsert，冲突时把 suffix 追加到 col_name 的既有值后面（日志式字段用）。"""
    stmt = _insert_stmt(model, values)
    column = getattr(model, col_name)
    expr = func.concat(func.coalesce(column, ""), "\n", suffix)
    return stmt.on_duplicate_key_update(**{col_name: expr})


def _ensure_indexes() -> None:
    """给已存在的表补索引 / 删冗余索引（幂等，失败只记日志）。

    ``create_all`` 不会改已有表的索引，所以老库要在这里补。
    """
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    with engine.begin() as conn:
        if "memory_outbox" in tables:
            names = {i["name"] for i in inspector.get_indexes("memory_outbox")}
            try:
                if "uq_outbox_session" not in names:
                    conn.execute(text(
                        "ALTER TABLE memory_outbox ADD UNIQUE KEY uq_outbox_session (session_id)"
                    ))
                if "idx_outbox_status" not in names:
                    conn.execute(text(
                        "ALTER TABLE memory_outbox ADD INDEX idx_outbox_status (status, id)"
                    ))
                if "idx_plan_user" in names:
                    conn.execute(text("ALTER TABLE memory_outbox DROP INDEX idx_plan_user"))
            except Exception as exc:  # noqa: BLE001 - 索引问题不该挡住启动
                log.warning("memory_outbox index fix skipped: %s", exc)

        if "chat_history" in tables:
            names = {i["name"] for i in inspector.get_indexes("chat_history")}
            try:
                if "idx_chat_user" in names:
                    conn.execute(text("ALTER TABLE chat_history DROP INDEX idx_chat_user"))
            except Exception as exc:  # noqa: BLE001
                log.warning("chat_history index fix skipped: %s", exc)


def init_db():
    """建表（幂等）+ 预填标准动作名 + 补用户档案列 + 补聊天语音列。"""
    Base.metadata.create_all(engine)
    # 延迟导入：seed 在 health 里，health 又依赖本模块，顶层导入会循环
    from .health import seed_exercise_names

    seed_exercise_names()
    _ensure_user_columns()
    _ensure_chat_columns()
    _ensure_strength_columns()
    _ensure_health_columns()
    _ensure_indexes()


def _ensure_user_columns():
    """为已存在的 users 表补充需求8的档案列（幂等）。"""
    inspector = inspect(engine)
    existing = {c["name"] for c in inspector.get_columns("users")}
    new_cols = {
        "height_cm": "FLOAT", "weight_kg": "FLOAT", "age": "INT",
        "gender": "VARCHAR(8)", "body_fat_pct": "FLOAT", "muscle_mass_kg": "FLOAT",
        "goal": "VARCHAR(32)", "goal_target": "VARCHAR(64)",
        "training_years": "FLOAT", "training_freq_per_week": "INT",
        "last_sync_date": "VARCHAR(10)",
        "last_sync_at": "VARCHAR(32)",
        # 2026-08-20 新增：训练类型 + 比赛列表（JSON TEXT）
        "training_types": "TEXT",
        "races": "TEXT",
    }
    with engine.begin() as conn:
        for col, typ in new_cols.items():
            if col not in existing:
                conn.execute(text(f"ALTER TABLE users ADD COLUMN {col} {typ}"))


def _ensure_chat_columns():
    """为已存在的 chat_history 表补充语音消息列（幂等）。"""
    inspector = inspect(engine)
    existing = {c["name"] for c in inspector.get_columns("chat_history")}
    new_cols = {
        "type": "VARCHAR(16) DEFAULT 'text'",
        "audio_url": "VARCHAR(255)",
        "duration_sec": "INT",
    }
    with engine.begin() as conn:
        for col, typ in new_cols.items():
            if col not in existing:
                conn.execute(text(f"ALTER TABLE chat_history ADD COLUMN {col} {typ}"))


def _ensure_strength_columns():
    """为已存在的 strength_sets 表补充器械/明细列（幂等）。"""
    inspector = inspect(engine)
    existing = {c["name"] for c in inspector.get_columns("strength_sets")}
    new_cols = {
        "reps_detail": "VARCHAR(128)",
        "equipment": "VARCHAR(64)",
    }
    with engine.begin() as conn:
        for col, typ in new_cols.items():
            if col not in existing:
                conn.execute(text(f"ALTER TABLE strength_sets ADD COLUMN {col} {typ}"))


def _ensure_health_columns():
    """为已存在的 health_daily 表补充后加列（幂等）。"""
    inspector = inspect(engine)
    existing = {c["name"] for c in inspector.get_columns("health_daily")}
    new_cols = {
        "body_battery_now": "INT",
    }
    with engine.begin() as conn:
        for col, typ in new_cols.items():
            if col not in existing:
                conn.execute(text(f"ALTER TABLE health_daily ADD COLUMN {col} {typ}"))


# ═══════════════════════════════════════════════
# 标准动作名（力量训练归一化）
# ═══════════════════════════════════════════════

DEFAULT_EXERCISES = [
    ("二头弯举", "举哑铃,哑铃弯举,杠铃弯举,弯举", "手臂"),
    ("三头下压", "三头,绳索下压,臂屈伸", "手臂"),
    ("肩推", "推肩,哑铃推举,杠铃推举,过头推举", "肩"),
    ("侧平举", "侧举,飞鸟", "肩"),
    ("前平举", "前举", "肩"),
    ("卧推", "平板卧推,杠铃卧推,哑铃卧推", "胸"),
    ("俯卧撑", "", "胸"),
    ("上斜卧推", "上斜推", "胸"),
    ("飞鸟", "哑铃飞鸟,夹胸", "胸"),
    ("引体向上", "引体,拉单杠", "背"),
    ("划船", "俯身划船,坐姿划船,杠铃划船", "背"),
    ("高位下拉", "下拉,宽握下拉", "背"),
    ("深蹲", "蹲,杠铃深蹲,哑铃深蹲,徒手深蹲", "腿"),
    ("硬拉", "罗马尼亚硬拉", "腿"),
    ("箭步蹲", "弓步蹲,弓箭步,箭步", "腿"),
    ("腿举", "倒蹬", "腿"),
    ("提踵", "踮脚", "腿"),
    ("卷腹", "仰卧起坐,腹肌,练腹", "核心"),
    ("平板支撑", "平板,核心支撑", "核心"),
    ("俄罗斯转体", "转体,俄式转体", "核心"),
]
