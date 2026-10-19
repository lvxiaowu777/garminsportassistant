"""
服务器端 Garmin 同步 —— 登录（两步 MFA）+ 从官方拉数据写 SQLite。

和本地 garmin_sync.py 的区别：不写 markdown，直接写服务器数据库，
按 user_id 隔离。token 存 config/.tokens/{user_id}/。
"""
import sys
import threading
import datetime as dt
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

SRC_DIR = Path(__file__).resolve().parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from garminconnect import Garmin
from garmin_sync import fetch_day
from database import upsert_health_daily

TOKEN_ROOT = Path(__file__).resolve().parent.parent / "config" / ".tokens"

# MFA 登录中暂存的 Garmin 对象。resume_login 依赖对象内部状态，必须保持同一实例。
# 同时暂存 account，供两步完成后写 watch_bindings。
_PENDING_MFA: dict[int, dict] = {}
_PENDING_LOCK = threading.Lock()


def _token_dir(user_id: int) -> Path:
    d = TOKEN_ROOT / str(user_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def login_and_store(user_id: int, email: str, password: str) -> tuple[bool, str | None]:
    """登录 Garmin 并存 token。返回 (needs_mfa, error)。"""
    try:
        g = Garmin(email, password, is_cn=True, return_on_mfa=True)
        mfa_status, _ = g.login()
        if mfa_status == "needs_mfa":
            with _PENDING_LOCK:
                _PENDING_MFA[user_id] = {"garmin": g, "account": email}
            return True, None
        g.client.dump(str(_token_dir(user_id)))
        return False, None
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:200]}"


def complete_mfa(user_id: int, mfa_code: str) -> tuple[bool, str | None, str | None]:
    """两步登录第二步：用验证码完成登录并存 token。返回 (ok, error, account)。"""
    with _PENDING_LOCK:
        pend = _PENDING_MFA.pop(user_id, None)
    if pend is None:
        return False, "登录会话已过期，请重新绑定", None
    g = pend["garmin"]
    account = pend["account"]
    try:
        g.resume_login(None, mfa_code)
        g.client.dump(str(_token_dir(user_id)))
        return True, None, account
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:200]}", None


def has_token(user_id: int) -> bool:
    return (_token_dir(user_id) / "garmin_tokens.json").exists()


def sync_user_garmin(user_id: int, days: int = 7) -> tuple[int, str | None]:
    """用已存 token 登录，拉最近 days 天数据写 SQLite。返回 (成功天数, error)。"""
    # 设备关闭则不同步
    from database import get_watch_enabled
    if not get_watch_enabled(user_id, "garmin"):
        return 0, "Garmin 设备已关闭，请先在设备页开启"

    try:
        g = Garmin(is_cn=True)
        g.login(str(_token_dir(user_id)))
    except Exception as e:
        return 0, f"Garmin 登录失败（token 可能过期，请重新绑定）: {str(e)[:150]}"

    ok = 0
    # 从今天开始拉（今天的心率/HRV/步数/活动实时可拉，睡眠今天为空、自然落到昨天）
    for i in range(0, days):
        d = (dt.date.today() - dt.timedelta(days=i)).isoformat()
        try:
            data = fetch_day(g, d)
            upsert_health_daily(data, user_id, "garmin")
            ok += 1
            print(f"  ✅ {d}")
        except Exception as e:
            print(f"  ⚠️ {d}: {type(e).__name__}: {str(e)[:120]}")
    return ok, None


def sync_all_users(days: int = 7) -> dict:
    """同步所有已绑定 garmin 的用户（定时任务用）。"""
    from database import get_enabled_user_ids
    uids = get_enabled_user_ids("garmin")

    result = {}
    for uid in uids:
        n, err = sync_user_garmin(uid, days)
        result[uid] = {"days": n, "error": err}
    return result
