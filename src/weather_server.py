"""
服务器版天气同步 —— 从和风拉天气直接写 SQLite。
必须传入经纬度定位当地天气（面向多人使用，不做默认城市）。

依赖（环境变量，由 llm_client 加载项目 .env 提供）：
    QWEATHER_API_KEY
    QWEATHER_API_HOST
"""
import sys
import os
import json
import datetime as dt
from pathlib import Path

import requests

SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from database import upsert_health_daily, get_active_brand


def _host() -> str:
    host = os.environ.get("QWEATHER_API_HOST", "").strip().rstrip("/")
    if not host:
        raise RuntimeError("缺少 QWEATHER_API_HOST")
    return host


def _key() -> str:
    k = os.environ.get("QWEATHER_API_KEY", "")
    if not k:
        raise RuntimeError("缺少 QWEATHER_API_KEY")
    return k


def _get(path: str, params: dict) -> dict:
    url = f"https://{_host()}{path}"
    r = requests.get(url, params={**params, "key": _key()}, timeout=15)
    r.raise_for_status()
    j = r.json()
    if "code" in j and j.get("code") != "200":
        raise RuntimeError(f"和风 code={j.get('code')}")
    return j


def _resolve(lon: float, lat: float) -> tuple[str, str, float, float]:
    """经纬度 → (LocationID, 城市名, lat, lon)。"""
    j = _get("/geo/v2/city/lookup", {"location": f"{lon},{lat}"})
    loc = (j.get("location") or [{}])[0]
    lid = loc.get("id")
    name = loc.get("name") or "当前位置"
    if not lid:
        raise RuntimeError(f"没找到位置 {lon},{lat}")
    return lid, name, float(loc.get("lat") or lat), float(loc.get("lon") or lon)


def _fetch(lid: str, lat: float, lon: float, date_str: str) -> dict:
    """给定 LocationID + 经纬度，拉一天核心天气。"""
    result = {}
    fc = _get("/v7/weather/3d", {"location": lid})
    for d in fc.get("daily", []):
        if d.get("fxDate") == date_str:
            result["desc"] = d.get("textDay")
            result["desc_night"] = d.get("textNight")
            result["high"] = int(d["tempMax"]) if d.get("tempMax") else None
            result["low"] = int(d["tempMin"]) if d.get("tempMin") else None
            result["humidity"] = int(d["humidity"]) if d.get("humidity") else None
            result["wind_kmh"] = int(d["windSpeedDay"]) if d.get("windSpeedDay") else None
            result["uv_index"] = int(d["uvIndex"]) if d.get("uvIndex") else None
            try:
                result["rain_prob"] = round(min(float(d.get("precip", "0")) / 10, 1), 2)
            except Exception:
                result["rain_prob"] = 0
            break

    today = dt.date.today().isoformat()
    if date_str == today:
        try:
            air = _get("/airquality/v1/current/{}/{}".format(lat, lon), {})
            indexes = air.get("indexes", []) if isinstance(air, dict) else []
            item = next((x for x in indexes if x.get("code") == "cn-mee"), None) or (indexes[0] if indexes else None)
            if item:
                result["aqi"] = int(item["aqi"]) if item.get("aqi") is not None else None
                result["aqi_category"] = item.get("category")
        except Exception as e:
            print(f"  ⚠️ AQI 失败: {e}")

        # 当前实时天气（温度/体感/是否在下雨）
        try:
            now = _get("/v7/weather/now", {"location": lid})
            n = now.get("now") or {}
            if n.get("temp"):
                result["now_temp"] = int(n["temp"])
            if n.get("feelsLike"):
                result["now_feels_like"] = int(n["feelsLike"])
            if n.get("text"):
                result["now_text"] = n["text"]
            if n.get("precip"):
                try:
                    result["now_precip"] = float(n["precip"])
                except Exception:
                    pass
        except Exception as e:
            print(f"  ⚠️ 实时天气失败: {e}")

    return result


def fetch_weather_by_coords(lon: float, lat: float, date_str: str) -> dict:
    """按经纬度拉当地天气，返回 weather dict（含 location 城市名）。"""
    lid, name, rlat, rlon = _resolve(lon, lat)
    weather = _fetch(lid, rlat, rlon, date_str)
    weather["location"] = name
    return weather


def sync_weather_to_db(user_id: int, date_str: str | None = None,
                       lon: float | None = None, lat: float | None = None) -> dict | None:
    """按经纬度拉当地天气并 upsert 到 SQLite。未定位返回 None。"""
    if not lon or not lat:
        return None
    date_str = date_str or dt.date.today().isoformat()
    weather = fetch_weather_by_coords(lon, lat, date_str)
    upsert_health_daily({"date": date_str, "weather": weather}, user_id, get_active_brand(user_id))
    return weather
