"""
服务器端 Coros（高驰）同步 —— 登录 + 从非官方 API 拉数据写 SQLite。

Coros 官方 API 未对个人开放。这里走社区逆向的非官方 API（cygnusb/coros-mcp 的实现）：

1. Web API（Training Hub，teamcnapi/teameuapi/teamapi）：
   - 登录：POST {base}/account/login  body={account, accountType:2, pwd: md5(password)}
   - 无验证码，token 24h。
   - /analyse/dayDetail/query → 每日 rhr / avgSleepHrv / trainingLoad / trainingLoadRatio
   - /analyse/query            → 近 28 天 vo2max / lthr / stamina
   - /activity/query           → 活动列表

2. Mobile API（apicn/apieu/api.coros.com）—— 睡眠数据在这，web API 拿不到：
   - 登录：POST {mobile_base}/coros/user/login，凭据 AES-128-CBC 加密（key/IV 从 APK 逆向）
   - /coros/data/statistic/daily → 睡眠阶段（深/浅/REM/清醒分钟）+ 睡眠心率

region 映射：cn/asia→teamcnapi(web)/apicn(mobile)，eu→teameuapi/apieu，us→teamapi/api。
token 存 config/.tokens/{user_id}/coros_auth.json（web token + mobile token + replay payload）。
"""
import sys
import json
import hashlib
import random
import time
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

import requests
from database import upsert_health_daily

TOKEN_ROOT = Path(__file__).resolve().parent.parent / "config" / ".tokens"

# Web API（Training Hub）
BASE_URLS = {
    "cn": "https://teamcnapi.coros.com",
    "asia": "https://teamcnapi.coros.com",
    "eu": "https://teameuapi.coros.com",
    "us": "https://teamapi.coros.com",
}

# Mobile API（睡眠数据）
MOBILE_BASE_URLS = {
    "cn": "https://apicn.coros.com",
    "asia": "https://apicn.coros.com",
    "eu": "https://apieu.coros.com",
    "us": "https://api.coros.com",
}

MOBILE_LOGIN_ENDPOINT = "/coros/user/login"
# AES-128-CBC IV，硬编码在 Coros APK 的 libencrypt-lib.so 里（逆向所得）
_MOBILE_AES_IV = b"weloop3_2015_03#"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)

# Coros sportType → 内部 type（前端用 type=="running" 统计跑步等）
SPORT_TYPE_MAP = {
    100: "running", 102: "running", 103: "running",      # 跑步 / 越野跑 / 跑道
    104: "hiking",
    200: "cycling", 201: "cycling", 203: "cycling", 204: "cycling",
    400: "cardio",
    402: "strength_training",
    403: "yoga",
    900: "walking",
}


def _md5(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def _base_url(region: str) -> str:
    return BASE_URLS.get(region, BASE_URLS["cn"])


def _mobile_base_url(region: str) -> str:
    return MOBILE_BASE_URLS.get(region, MOBILE_BASE_URLS["cn"])


def _token_dir(user_id: int) -> Path:
    d = TOKEN_ROOT / str(user_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _token_file(user_id: int) -> Path:
    return _token_dir(user_id) / "coros_auth.json"


def _save_auth(user_id: int, auth: dict):
    auth["timestamp"] = int(time.time() * 1000)
    _token_file(user_id).write_text(json.dumps(auth, ensure_ascii=False), encoding="utf-8")


def _load_auth(user_id: int) -> dict | None:
    f = _token_file(user_id)
    if not f.exists():
        return None
    try:
        auth = json.loads(f.read_text(encoding="utf-8"))
        # web token 24h 有效
        if int(time.time() * 1000) - auth.get("timestamp", 0) > 24 * 3600 * 1000:
            return None
        return auth
    except Exception:
        return None


def has_token(user_id: int) -> bool:
    return _load_auth(user_id) is not None


# ═══════════════════════════════════════════════
# Mobile API 加密登录（睡眠数据用）
# ═══════════════════════════════════════════════

def _mobile_encrypt(plaintext: str, app_key: str) -> str:
    """加密字符串用于 mobile 登录。XOR + PKCS7 + AES-128-CBC + base64。"""
    import base64
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = app_key.encode("ascii")
    data = plaintext.encode("utf-8")
    xored = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
    pad_len = 16 - (len(xored) % 16)
    padded = xored + bytes([pad_len] * pad_len)
    encryptor = Cipher(algorithms.AES(key), modes.CBC(_MOBILE_AES_IV)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(ciphertext).decode("ascii")


def _mobile_login(email: str, password: str, region: str) -> tuple[str, dict]:
    """登录 Coros mobile API，返回 (access_token, login_payload)。"""
    url = _mobile_base_url(region) + MOBILE_LOGIN_ENDPOINT
    app_key = str(random.randint(1_000_000_000_000_000, 9_999_999_999_999_999))
    payload = {
        "account": _mobile_encrypt(email, app_key) + "\n",
        "accountType": 2,
        "appKey": app_key,
        "clientType": 1,
        "hasHrCalibrated": 0,
        "kbValidity": 0,
        "pwd": _mobile_encrypt(_md5(password), app_key) + "\n",
        "region": "310|Europe/Berlin|US",  # 设备 SIM 遥测，非账号区服，照抄
        "skipValidation": False,
    }
    # Android 设备指纹（mitmproxy 抓的）。Coros 若拒旧客户端需手动更新。
    yfheader = json.dumps({
        "appVersion": 1125917087236096,
        "clientType": 1,
        "language": "en-US",
        "mobileName": "sdk_gphone64_arm64,google,Google",
        "releaseType": 1,
        "systemVersion": "13",
        "timezone": 4,
        "versionCode": "404080400",
    }, separators=(",", ":"))
    headers = {
        "content-type": "application/json",
        "accept-encoding": "gzip",
        "user-agent": "okhttp/4.12.0",
        "request-time": str(int(time.time() * 1000)),
        "yfheader": yfheader,
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=30)
    body = resp.json()
    _check(body, "mobile login")
    token = body.get("data", {}).get("accessToken")
    if not token:
        raise RuntimeError("mobile login 无 accessToken")
    return token, payload


# ═══════════════════════════════════════════════
# 登录
# ═══════════════════════════════════════════════

def login_and_store(user_id: int, email: str, password: str, region: str = "cn") -> tuple[bool, str | None]:
    """登录 Coros（web + mobile）并存 token。返回 (ok, error)。"""
    url = _base_url(region) + "/account/login"
    payload = {
        "account": email,
        "accountType": 2,
        "pwd": _md5(password),
    }
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        body = resp.json()
        if body.get("result") != "0000":
            return False, f"Coros 登录失败: {body.get('message', '未知错误')}"
        data = body.get("data", {})
        access_token = data.get("accessToken")
        if not access_token:
            return False, "登录成功但无 accessToken"

        auth = {
            "access_token": access_token,
            "user_id": str(data.get("userId", "")),
            "region": region,
            "mobile_access_token": None,
            "mobile_login_payload": None,
        }
        # mobile login 拿睡眠数据 token，失败不阻塞（睡眠拉不到，其余正常）
        try:
            mtoken, mpayload = _mobile_login(email, password, region)
            auth["mobile_access_token"] = mtoken
            auth["mobile_login_payload"] = mpayload
        except Exception as e:
            print(f"  ⚠️ mobile 登录失败（睡眠数据将缺失）: {type(e).__name__}: {str(e)[:120]}")

        _save_auth(user_id, auth)
        return True, None
    except requests.RequestException as e:
        return False, f"网络错误: {type(e).__name__}: {str(e)[:150]}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:150]}"


# ═══════════════════════════════════════════════
# 拉数据
# ═══════════════════════════════════════════════

def _auth_headers(auth: dict) -> dict:
    return {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "accessToken": auth["access_token"],
        "yfheader": json.dumps({"userId": auth.get("user_id", "")}),
    }


def _check(body: dict, context: str):
    if body.get("result") != "0000":
        raise RuntimeError(f"Coros {context} error: {body.get('message', 'unknown')}")


def _norm_date(s) -> str:
    """把 Coros 的 happenDay 统一成 YYYY-MM-DD（兼容 YYYYMMDD 和 YYYY-MM-DD）。"""
    s = str(s).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def _fetch_daily_records(auth: dict, start_day: str, end_day: str) -> dict[str, dict]:
    """拉每日指标，返回 {date: record}。合并 dayDetail（长范围）+ analyse（近28天 vo2max）。"""
    base = _base_url(auth["region"])
    headers = _auth_headers(auth)
    s = requests.Session()

    detail = s.get(
        base + "/analyse/dayDetail/query",
        params={"startDay": start_day, "endDay": end_day},
        headers=headers, timeout=30,
    ).json()
    _check(detail, "dayDetail")

    records: dict[str, dict] = {}
    for item in detail.get("data", {}).get("dayList", []):
        d = _norm_date(item.get("happenDay", ""))
        if not d:
            continue
        records[d] = item

    # 近 28 天 VO2max / stamina 从 /analyse/query 合并
    try:
        analyse = s.get(base + "/analyse/query", headers=headers, timeout=30).json()
        if analyse.get("result") == "0000":
            for item in analyse.get("data", {}).get("t7dayList", []):
                d = _norm_date(item.get("happenDay", ""))
                if d in records:
                    for k in ("vo2max", "lthr", "ltsp", "staminaLevel", "staminaLevel7d"):
                        if item.get(k) is not None:
                            records[d][k] = item[k]
    except Exception:
        pass  # vo2max 合并失败不影响核心指标

    return records


def _ts_to_date(ts) -> str | None:
    """Coros 返回毫秒时间戳 → 本地日期字符串。"""
    try:
        ts = int(ts)
        if ts > 10_000_000_000:
            ts //= 1000
        return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except Exception:
        return None


def _ts_to_time(ts) -> str | None:
    try:
        ts = int(ts)
        if ts > 10_000_000_000:
            ts //= 1000
        return dt.datetime.fromtimestamp(ts).strftime("%H:%M")
    except Exception:
        return None


def _fetch_activities(auth: dict, start_day: str, end_day: str) -> dict[str, list[dict]]:
    """拉活动列表，按日期分组返回 {date: [activity]}。"""
    base = _base_url(auth["region"])
    headers = _auth_headers(auth)
    result: dict[str, list[dict]] = {}

    page = 1
    while True:
        resp = requests.get(
            base + "/activity/query",
            params={"startDay": start_day, "endDay": end_day, "pageNumber": page, "size": 30},
            headers=headers, timeout=30,
        ).json()
        _check(resp, "activity list")
        data = resp.get("data", {})
        items = data.get("dataList", data.get("list", []))
        if not items:
            break
        for item in items:
            act = _parse_activity(item)
            d = act.get("date")
            if d:
                result.setdefault(d, []).append(act)
        total = data.get("totalCount") or 0
        if page * 30 >= total:
            break
        page += 1

    return result


def _parse_activity(item: dict) -> dict:
    """Coros activity item → 内部 activity dict（对齐 garmin_sync 的字段）。"""
    sport_type = item.get("sportType")
    start_ts = item.get("startTime")
    return {
        "id": str(item.get("labelId", "")),
        "date": _ts_to_date(start_ts),
        "time": _ts_to_time(start_ts),
        "type": SPORT_TYPE_MAP.get(sport_type, f"sport_{sport_type}" if sport_type else None),
        "location": item.get("remark") or item.get("name"),
        "distance_km": round((item.get("distance") or item.get("totalDistance") or 0) / 1000, 2),
        "duration_min": round((item.get("totalTime") or 0) / 60, 2),
        "avg_hr": item.get("avgHr"),
        "max_hr": item.get("maxHr"),
        # Coros 的 calorie 单位是 cal（物理卡），不是 kcal，要 /1000
        "calories": round((item.get("calorie") or 0) / 1000) if item.get("calorie") else None,
        "training_load": item.get("trainingLoad"),
        "elevation_gain_m": item.get("ascent") or item.get("totalAscent"),
        "elevation_loss_m": item.get("descent") or item.get("totalDescent"),
    }


def _compact(date_str: str) -> int:
    """YYYY-MM-DD → YYYYMMDD int（mobile API 要这个格式）。"""
    return int(date_str.replace("-", ""))


def _refresh_mobile_token(auth: dict) -> bool:
    """token 过期时 replay 加密 payload 刷新 mobile token（无需重输密码）。"""
    payload = auth.get("mobile_login_payload")
    if not payload:
        return False
    url = _mobile_base_url(auth["region"]) + MOBILE_LOGIN_ENDPOINT
    headers = {
        "content-type": "application/json",
        "accept-encoding": "gzip",
        "user-agent": "okhttp/4.12.0",
        "request-time": str(int(time.time() * 1000)),
    }
    try:
        body = requests.post(url, json=payload, headers=headers, timeout=30).json()
        if body.get("result") != "0000":
            return False
        token = body.get("data", {}).get("accessToken")
        if not token:
            return False
        auth["mobile_access_token"] = token
        return True
    except Exception:
        return False


def _fetch_sleep(auth: dict, start_day: str, end_day: str) -> dict[str, dict]:
    """拉睡眠数据（mobile API），返回 {date: sleep_dict}。"""
    token = auth.get("mobile_access_token")
    if not token:
        return {}

    url = _mobile_base_url(auth["region"]) + "/coros/data/statistic/daily"
    sleep_payload = {
        "allDeviceSleep": 1,
        "dataType": [5],
        "dataVersion": 0,
        "startTime": _compact(start_day),
        "endTime": _compact(end_day),
        "statisticType": 1,
    }

    def _do(token: str) -> dict:
        resp = requests.post(
            url,
            params={"accessToken": token},
            json=sleep_payload,
            headers={"Content-Type": "application/json", "accesstoken": token},
            timeout=30,
        )
        return resp.json()

    body = _do(token)
    if body.get("result") == "1019":  # token 过期，replay 刷新一次
        if _refresh_mobile_token(auth):
            body = _do(auth["mobile_access_token"])
    _check(body, "sleep")

    result: dict[str, dict] = {}
    for item in body.get("data", {}).get("statisticData", {}).get("dayDataList", []):
        d = _norm_date(item.get("happenDay", ""))
        if not d:
            continue
        sd = item.get("sleepData", {}) or {}
        quality = item.get("performance")
        sleep = {}
        if sd.get("totalSleepTime") is not None:
            sleep["hours"] = round(sd["totalSleepTime"] / 60, 1)
        if sd.get("deepTime") is not None:
            sleep["deep_min"] = sd["deepTime"]
        if sd.get("eyeTime") is not None:  # Coros 用 eyeTime 表示 REM
            sleep["rem_min"] = sd["eyeTime"]
        if sd.get("lightTime") is not None:
            sleep["light_min"] = sd["lightTime"]
        if quality is not None and quality != -1:
            sleep["score"] = quality
        if sd.get("wakeTime") is not None:
            sleep["feedback"] = f"清醒 {sd['wakeTime']} 分钟"
        if sleep:
            result[d] = sleep
    return result


# ═══════════════════════════════════════════════
# 同步入口
# ═══════════════════════════════════════════════

def _build_day(date_str: str, rec: dict | None, activities: list[dict], sleep: dict | None) -> dict:
    """把 Coros 一天的 record + activities + sleep 组装成 upsert_health_daily 期望的格式。"""
    rec = rec or {}
    recovery = {}
    fitness = {}

    if rec.get("rhr") is not None:
        recovery["resting_hr"] = int(rec["rhr"])
    if rec.get("avgSleepHrv") is not None:
        recovery["hrv"] = int(rec["avgSleepHrv"])
    if rec.get("sleepHrvBase") is not None:
        recovery["hrv_baseline_low"] = int(rec["sleepHrvBase"])

    if rec.get("trainingLoad") is not None:
        fitness["acute_load"] = rec["trainingLoad"]
    if rec.get("trainingLoadRatio") is not None:
        fitness["acwr_ratio"] = rec["trainingLoadRatio"]
    if rec.get("vo2max") is not None:
        fitness["vo2max"] = rec["vo2max"]
    if rec.get("staminaLevel") is not None:
        fitness["training_feedback"] = f"体能 {rec['staminaLevel']}/5"

    d = dt.date.fromisoformat(date_str)
    data = {
        "date": date_str,
        "weekday": ["一", "二", "三", "四", "五", "六", "日"][d.weekday()],
    }
    if recovery:
        data["recovery"] = recovery
    if fitness:
        data["fitness"] = fitness
    if sleep:
        data["sleep"] = sleep
    if activities:
        data["activities"] = activities
    return data


def sync_user_coros(user_id: int, days: int = 7) -> tuple[int, str | None]:
    """用已存 token 拉最近 days 天数据写 MySQL。返回 (成功天数, error)。"""
    from database import get_watch_enabled
    if not get_watch_enabled(user_id, "coros"):
        return 0, "Coros 设备已关闭，请先在设备页开启"

    auth = _load_auth(user_id)
    if not auth:
        return 0, "Coros token 缺失或过期，请重新绑定"

    today = dt.date.today()
    start = today - dt.timedelta(days=days)
    # Coros API 要 YYYYMMDD（8 位数字），不是 YYYY-MM-DD
    start_day = start.strftime("%Y%m%d")
    end_day = today.strftime("%Y%m%d")

    try:
        records = _fetch_daily_records(auth, start_day, end_day)
    except Exception as e:
        return 0, f"Coros 每日指标拉取失败: {str(e)[:150]}"
    try:
        acts_by_date = _fetch_activities(auth, start_day, end_day)
    except Exception as e:
        acts_by_date = {}
        print(f"  ⚠️ 活动拉取失败: {str(e)[:120]}")
    try:
        sleep_by_date = _fetch_sleep(auth, start_day, end_day)
    except Exception as e:
        sleep_by_date = {}
        print(f"  ⚠️ 睡眠拉取失败: {str(e)[:120]}")

    ok = 0
    for i in range(0, days):
        d = (today - dt.timedelta(days=i)).isoformat()
        rec = records.get(d)
        acts = acts_by_date.get(d, [])
        sleep = sleep_by_date.get(d)
        if rec is None and not acts and sleep is None:
            continue
        data = _build_day(d, rec, acts, sleep)
        upsert_health_daily(data, user_id, "coros")
        ok += 1
        print(f"  ✅ {d}")
    return ok, None


def sync_all_coros_users(days: int = 7) -> dict:
    """同步所有已绑定 coros 的用户（定时任务用）。"""
    from database import get_enabled_user_ids
    uids = get_enabled_user_ids("coros")

    result = {}
    for uid in uids:
        n, err = sync_user_coros(uid, days)
        result[uid] = {"days": n, "error": err}
    return result
