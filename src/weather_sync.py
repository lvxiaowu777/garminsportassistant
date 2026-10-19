"""
和风天气客户端 —— 拉襄阳当日天气 + 逐小时预报 + AQI，
合并进 知识库/健康/YYYY-MM/YYYY-MM-DD.md 的 frontmatter。

依赖：
    study/.env 里
    QWEATHER_API_KEY=xxx
    QWEATHER_API_HOST=xxx.re.qweatherapi.com   # 付费版专属域名，必填

用法：
    python weather_sync.py                # 默认今天
    python weather_sync.py 2026-07-10

数据结构（frontmatter 里的 weather:）：
    weather:
      desc: "多云"                # 白天天气
      desc_night: "小雨"           # 晚上天气
      high: 35
      low: 27
      humidity: 65                 # 平均湿度
      rain_prob: 0.3               # 白天降水概率
      wind_kmh: 12
      uv_index: 8
      aqi: 82                      # 空气质量指数
      aqi_category: "良"
      run_windows:                 # 跑步友好时段（小时列表）
        - {hour: 6, temp: 27, humidity: 78, feels_like: 28, precip_mm: 0}
        - {hour: 20, temp: 30, humidity: 70, feels_like: 32, precip_mm: 0}
      location: "襄阳"
"""

import sys
import json
import datetime as dt
from pathlib import Path

import requests

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, r"C:\Users\boobyboy\study")
from llm_env import get_key  # noqa: E402

STUDY_DIR = Path(r"C:\Users\boobyboy\study")
HEALTH_DIR = STUDY_DIR / "知识库" / "健康"

CITY_NAME = "襄阳"
LOCATION_LABEL = "襄阳"
CACHE_FILE = Path(__file__).resolve().parent.parent / "config" / "location_cache.json"


# ─────────── 和风底层调用 ───────────

def _api_host() -> str:
    host = get_key("QWEATHER_API_HOST")
    if not host:
        raise RuntimeError(
            "缺少 QWEATHER_API_HOST，付费版和风必须配置专属 API Host。\n"
            "格式类似 xxx.re.qweatherapi.com，加到 study/.env"
        )
    return host.strip().rstrip("/")


def _key() -> str:
    k = get_key("QWEATHER")
    if not k:
        raise RuntimeError("缺少 QWEATHER_API_KEY，加到 study/.env")
    return k


def _qw_get(path: str, params: dict) -> dict:
    """
    和风统一 GET 接口。
    - v7 系列（/v7/weather/*, /geo/v2/*）走 code 校验
    - v1 新版接口（/airquality/v1/*）直接看 HTTP 状态和 payload
    """
    host = _api_host()
    url = f"https://{host}{path}"
    params = {**params, "key": _key()}
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    j = r.json()
    # v7 系列有 code 字段
    if "code" in j and j.get("code") != "200":
        raise RuntimeError(f"和风 API 错误 code={j.get('code')} @ {path} :: {j}")
    return j


# ─────────── LocationID 查询（缓存） ───────────

def get_location_id(name: str = CITY_NAME) -> str:
    """
    查城市 LocationID。第一次网络查询，之后缓存到 config/location_cache.json。
    """
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    cache = {}
    if CACHE_FILE.exists():
        try:
            cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            cache = {}
    if name in cache:
        return cache[name]

    print(f"→ 首次查询城市 ID: {name}")
    j = _qw_get("/geo/v2/city/lookup", {"location": name})
    loc = (j.get("location") or [{}])[0]
    lid = loc.get("id")
    if not lid:
        raise RuntimeError(f"没找到城市 {name}：{j}")
    print(f"  {name} → id={lid} ({loc.get('adm2')}/{loc.get('adm1')})")
    cache[name] = lid
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    return lid


# ─────────── 数据组装 ───────────

def _find_forecast_for_date(forecast_days: list, date_str: str) -> dict | None:
    """在 3d/7d 预报的 daily 列表里挑指定日期。"""
    for d in forecast_days:
        if d.get("fxDate") == date_str:
            return d
    return None


def _pick_run_windows(hourly: list) -> list:
    """
    从逐小时预报里挑跑步友好的时段。
    条件：无雨（precip=0）且体感<32°C 或 是清晨/晚上（6-9 / 18-22）
    """
    def _feels_like(temp, hum):
        try:
            t = float(temp)
            h = float(hum)
        except (TypeError, ValueError):
            return None
        # 简化的湿热指数（华氏公式转摄氏近似）
        # 体感 ≈ 温度 + 0.05 * 湿度（比较粗糙但够本项目排序用）
        return round(t + 0.05 * h, 1)

    windows = []
    for h in hourly:
        fx = h.get("fxTime", "")
        try:
            hour = int(fx[11:13])
        except Exception:
            continue
        # 只关心当天（丢弃跨天）
        if hour < 5 or hour > 22:
            continue
        precip = float(h.get("precip", "0") or 0)
        temp = h.get("temp")
        hum = h.get("humidity")
        try:
            t = int(temp)
            hu = int(hum)
        except (TypeError, ValueError):
            continue
        feels = _feels_like(t, hu)
        # 无雨 且 (体感<32 或 清晨/傍晚)
        friendly = precip == 0 and (feels is None or feels < 32 or hour <= 8 or hour >= 19)
        if friendly:
            windows.append({
                "hour": hour,
                "temp": t,
                "humidity": hu,
                "feels_like": feels,
                "precip_mm": precip,
            })
    return windows[:6]  # 最多列 6 个


def fetch_weather(date_str: str) -> dict:
    """
    拉一天的完整天气数据（预报 + 逐小时 + 空气质量）。
    """
    lid = get_location_id(CITY_NAME)
    print(f"→ 拉和风天气 date={date_str} location={lid}")

    result = {"location": LOCATION_LABEL}

    # 1) 3天预报（拿目标日期的白天/晚上/最高最低温）
    fc = _qw_get("/v7/weather/3d", {"location": lid})
    day = _find_forecast_for_date(fc.get("daily", []), date_str)
    if day:
        result["desc"] = day.get("textDay")
        result["desc_night"] = day.get("textNight")
        result["high"] = int(day.get("tempMax", 0)) if day.get("tempMax") else None
        result["low"] = int(day.get("tempMin", 0)) if day.get("tempMin") else None
        result["humidity"] = int(day.get("humidity", 0)) if day.get("humidity") else None
        # 降水概率
        try:
            precip_mm = float(day.get("precip", "0"))
            result["precip_mm"] = precip_mm
            result["rain_prob"] = round(min(precip_mm / 10, 1), 2) if precip_mm > 0 else 0
        except (TypeError, ValueError):
            result["rain_prob"] = 0
        result["wind_kmh"] = int(day.get("windSpeedDay", 0)) if day.get("windSpeedDay") else None
        result["uv_index"] = int(day.get("uvIndex", 0)) if day.get("uvIndex") else None
        result["sunrise"] = day.get("sunrise")
        result["sunset"] = day.get("sunset")

    # 2) 24小时逐小时预报（只有今天有意义）
    today = dt.date.today().isoformat()
    if date_str == today:
        hourly = _qw_get("/v7/weather/24h", {"location": lid})
        result["run_windows"] = _pick_run_windows(hourly.get("hourly", []))

    # 3) 空气质量（新版 v1 接口需要经纬度，襄阳 32.05/112.13）
    if date_str == today:
        try:
            air = _qw_get("/airquality/v1/current/32.05/112.13", {})
            # v1 接口把结果包在 indexes 数组里，取 cn-mee (中国标准)
            indexes = air.get("indexes", []) if isinstance(air, dict) else []
            aqi_item = next((x for x in indexes if x.get("code") == "cn-mee"), None) or (indexes[0] if indexes else None)
            if aqi_item:
                result["aqi"] = int(aqi_item.get("aqi")) if aqi_item.get("aqi") is not None else None
                result["aqi_category"] = aqi_item.get("category")
                result["primary_pollutant"] = aqi_item.get("primaryPollutant") or "无"
        except Exception as e:
            print(f"  ⚠️  空气质量接口挂了: {type(e).__name__}: {e}")

    return result


# ─────────── 落到 md（复用之前的逻辑）───────────

def _fmt_weather_block(weather: dict) -> str:
    """把 weather dict 序列化成 YAML 段（顶层 weather: 加缩进）。"""
    lines = ["weather:"]
    for k, v in weather.items():
        if v is None:
            continue
        if isinstance(v, str):
            lines.append(f'  {k}: "{v}"')
        elif isinstance(v, list):
            # run_windows: list of inline dict
            lines.append(f"  {k}:")
            for item in v:
                parts = [f'{ik}: {json.dumps(iv, ensure_ascii=False)}' for ik, iv in item.items() if iv is not None]
                lines.append(f"    - {{{', '.join(parts)}}}")
        else:
            lines.append(f"  {k}: {v}")
    return "\n".join(lines)


def merge_weather_into_md(date_str: str, weather: dict) -> Path:
    """把 weather 段合并进当日健康 md 的 frontmatter。"""
    year_month = date_str[:7]
    out_dir = HEALTH_DIR / year_month
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{date_str}.md"

    weather_block = _fmt_weather_block(weather)

    if not out_path.exists():
        content = (
            "---\n"
            f"date: {date_str}\n"
            f"{weather_block}\n"
            "---\n\n"
            f"# 健康数据 · {date_str}\n\n"
            "> 由 [[weather_sync]] 先落地。等 [[garmin_sync]] 补身体数据。\n"
        )
        out_path.write_text(content, encoding="utf-8")
        print(f"✅ 新建: {out_path}")
        return out_path

    text = out_path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        new_text = f"---\n{weather_block}\n---\n\n" + text
        out_path.write_text(new_text, encoding="utf-8")
        return out_path

    fm_end = text.find("---", 3)
    if fm_end < 0:
        new_text = f"---\n{weather_block}\n---\n\n" + text
        out_path.write_text(new_text, encoding="utf-8")
        return out_path

    fm = text[3:fm_end]
    rest = text[fm_end:]

    # 去掉 fm 里已有的 weather 段
    fm_lines = fm.split("\n")
    filtered = []
    skip = False
    for ln in fm_lines:
        if ln.startswith("weather:"):
            skip = True
            continue
        if skip:
            if ln.startswith("  ") or ln.startswith("    ") or ln.strip() == "":
                continue
            skip = False
        filtered.append(ln)

    new_fm = "\n".join(l for l in filtered).rstrip() + "\n" + weather_block + "\n"
    new_text = "---" + "\n" + new_fm.lstrip("\n") + rest
    out_path.write_text(new_text, encoding="utf-8")
    print(f"✅ 合并 weather 到: {out_path}")
    return out_path


def sync_weather(date_str: str | None = None, force: bool = False) -> Path:
    """拉天气并合并进当日 md。force 参数保留但不做限流（付费版 50000次/月够用）。"""
    if date_str is None:
        date_str = dt.date.today().isoformat()

    weather = fetch_weather(date_str)
    desc = weather.get('desc') or '?'
    if weather.get('desc_night') and weather['desc_night'] != desc:
        desc = f"{desc}转{weather['desc_night']}"
    print(
        f"  {LOCATION_LABEL} · {desc} · {weather.get('low', '?')}~{weather.get('high', '?')}°C · "
        f"湿度{weather.get('humidity', '?')}% · AQI={weather.get('aqi', '?')}"
    )
    if weather.get("run_windows"):
        w = weather["run_windows"]
        hours = ", ".join(f"{x['hour']:02d}点({x['temp']}°C)" for x in w[:4])
        print(f"  跑步友好时段: {hours}")

    return merge_weather_into_md(date_str, weather)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?", help="YYYY-MM-DD，默认今天")
    ap.add_argument("--force", action="store_true", help="（保留兼容，无实际作用）")
    args = ap.parse_args()
    try:
        path = sync_weather(args.date, force=args.force)
        print(f"\n✅ 完成: {path}")
    except Exception as e:
        print(f"❌ 出错: {type(e).__name__}: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)
