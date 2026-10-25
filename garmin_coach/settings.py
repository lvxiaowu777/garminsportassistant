"""应用级配置：**所有可调参数只在这里出现一次**。

原则：
- 代码里不写魔法数字，一律 ``settings.xxx``；
- 默认值给开发环境用，生产用环境变量覆盖（``.env`` 或容器 env）；
- 只放"运行时行为"参数；密钥类（API key / DATABASE_URL / JWT_SECRET）
  仍留在各自的模块读（``llm/config.py``、``database.py``、``auth.py``），
  避免一次性大搬家引入回归。

用法::

    from garmin_coach.settings import settings
    settings.session_ttl_seconds
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# 项目根目录的 .env（不依赖调用方先 export）
try:  # dotenv 是可选依赖，缺席时只读真实环境变量
    from dotenv import load_dotenv

    _ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
    if _ENV_FILE.exists():
        load_dotenv(_ENV_FILE, override=False)
except ImportError:  # pragma: no cover
    pass


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"环境变量 {name} 必须是整数，当前值：{raw!r}") from None


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"环境变量 {name} 必须是数字，当前值：{raw!r}") from None


@dataclass(frozen=True)
class Settings:
    # ── 会话 ──
    session_ttl_seconds: int = 3600        # Agent 空闲多久回收（秒）
    max_sessions: int = 200                # 内存里最多驻留多少个会话（LRU）
    session_reap_interval_seconds: int = 300  # 后台扫描间隔（秒）

    # ── LLM ──
    llm_provider: str = "deepseek"     # 后端管理员决定用哪家（LLM_PROVIDER）

    # ── Agent ──
    agent_max_turns: int = 8               # 一轮对话最多几次「模型 ↔ 工具」
    agent_temperature: float = 0.7

    # ── 同步 ──
    sync_days_default: int = 2             # 日常同步拉几天（首次绑表才 backfill）
    sync_fresh_ttl_minutes: int = 10       # 对话前：数据比这个旧就先同步一次
    sync_fresh_timeout_seconds: int = 25   # 对话前同步的超时（超时就先用旧数据）
    sync_preflight_enabled: bool = True    # 对话前是否自动同步（评测/离线环境可关）


def load_settings() -> Settings:
    return Settings(
        llm_provider=os.environ.get("LLM_PROVIDER", "deepseek").strip() or "deepseek",
        session_ttl_seconds=_int("SESSION_TTL_SECONDS", 3600),
        max_sessions=_int("MAX_SESSIONS", 200),
        session_reap_interval_seconds=_int("SESSION_REAP_INTERVAL_SECONDS", 300),
        agent_max_turns=_int("AGENT_MAX_TURNS", 8),
        agent_temperature=_float("AGENT_TEMPERATURE", 0.7),
        sync_days_default=_int("SYNC_DAYS_DEFAULT", 2),
        sync_fresh_ttl_minutes=_int("SYNC_FRESH_TTL_MINUTES", 10),
        sync_fresh_timeout_seconds=_int("SYNC_FRESH_TIMEOUT_SECONDS", 25),
        sync_preflight_enabled=os.environ.get("SYNC_PREFLIGHT_ENABLED", "1").strip()
        not in ("0", "false", "False"),
    )


#: 全局单例。测试想改就用 monkeypatch 改属性（frozen 时重建一个）。
settings = load_settings()
