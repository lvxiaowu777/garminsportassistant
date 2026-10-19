"""
共享 YAML frontmatter 解析/序列化模块。

cli.py 和 garmin_sync.py 都调这个，改格式只改一处。
"""

import json
from pathlib import Path


# ═══════════════════════════════════════════════
# 解析（读 .md → dict）
# ═══════════════════════════════════════════════

def _cast(v: str):
    v = v.strip()
    if v == "null" or v == "":
        return None
    if v in ("true", "false"):
        return v == "true"
    if v.startswith('"') and v.endswith('"'):
        try:
            return json.loads(v)
        except Exception:
            return v[1:-1]
    try:
        if "." in v:
            return float(v)
        return int(v)
    except ValueError:
        return v


def _parse_inline_dict(s: str) -> dict:
    """解析 "{k: v, k: v}" 单行。"""
    s = s.strip()
    if s.startswith("{"):
        s = s[1:]
    if s.endswith("}"):
        s = s[:-1]
    result = {}
    depth = 0
    buf = ""
    parts = []
    for ch in s:
        if ch == "{":
            depth += 1
            buf += ch
        elif ch == "}":
            depth -= 1
            buf += ch
        elif ch == "," and depth == 0:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    if buf.strip():
        parts.append(buf)
    for part in parts:
        if ":" not in part:
            continue
        k, _, v = part.partition(":")
        result[k.strip()] = _cast(v.strip())
    return result


def parse(path: Path) -> dict:
    """
    读一份健康 md 的 frontmatter。
    支持顶层 inline dict、多行 dict、activities 列表（带 splits/hr_zones 嵌套）。
    """
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    end = text.find("---", 3)
    if end < 0:
        return {}
    fm = text[3:end]

    result: dict = {}
    lines = fm.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.rstrip()
        i += 1
        if not stripped or stripped.startswith("#"):
            continue
        if not stripped.startswith(" "):
            if ":" not in stripped:
                continue
            k, _, v = stripped.partition(":")
            k = k.strip()
            v = v.strip()
            if v == "":
                if i < len(lines) and lines[i].lstrip().startswith("- "):
                    lst = []
                    while i < len(lines) and (lines[i].startswith("  ") or lines[i].strip() == ""):
                        sub = lines[i]
                        if sub.strip().startswith("- "):
                            item = {}
                            first = sub.strip()[2:]
                            if ":" in first:
                                fk, _, fv = first.partition(":")
                                item[fk.strip()] = _cast(fv.strip())
                            i += 1
                            while i < len(lines) and lines[i].startswith("    "):
                                inner = lines[i][4:]
                                if inner.startswith("  "):
                                    if "splits" in item and isinstance(item["splits"], list):
                                        if inner.strip().startswith("- "):
                                            item["splits"].append(_parse_inline_dict(inner.strip()[2:]))
                                    i += 1
                                    continue
                                if ":" not in inner:
                                    i += 1
                                    continue
                                ik, _, iv = inner.partition(":")
                                ik = ik.strip()
                                iv = iv.strip()
                                if iv == "" and ik == "splits":
                                    item["splits"] = []
                                    i += 1
                                    while i < len(lines) and lines[i].startswith("      - "):
                                        item["splits"].append(_parse_inline_dict(lines[i].strip()[2:]))
                                        i += 1
                                    continue
                                if iv.startswith("{"):
                                    item[ik] = _parse_inline_dict(iv)
                                else:
                                    item[ik] = _cast(iv)
                                i += 1
                            lst.append(item)
                        else:
                            i += 1
                    result[k] = lst
                else:
                    d = {}
                    while i < len(lines) and lines[i].startswith("  ") and not lines[i].startswith("    "):
                        inner = lines[i][2:]
                        if ":" not in inner:
                            i += 1
                            continue
                        ik, _, iv = inner.partition(":")
                        ik = ik.strip()
                        iv = iv.strip()
                        if iv.startswith("{"):
                            d[ik] = _parse_inline_dict(iv)
                        elif iv == "":
                            i += 1
                            if i < len(lines) and lines[i].startswith("    - "):
                                sub_lst = []
                                while i < len(lines) and lines[i].startswith("    - "):
                                    item_str = lines[i].strip()[2:]
                                    if item_str.startswith("{"):
                                        sub_lst.append(_parse_inline_dict(item_str))
                                    i += 1
                                d[ik] = sub_lst
                            else:
                                d[ik] = None
                            continue
                        else:
                            d[ik] = _cast(iv)
                        i += 1
                    result[k] = d
            elif v == "[]":
                result[k] = []
            elif v.startswith("{"):
                result[k] = _parse_inline_dict(v)
            elif v.startswith("["):
                result[k] = []
            else:
                result[k] = _cast(v)
    return result


# ═══════════════════════════════════════════════
# 序列化（dict → YAML frontmatter 字符串）
# ═══════════════════════════════════════════════

def _fmt_scalar(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        if isinstance(v, float) and v == int(v):
            return str(int(v))
        return str(v)
    s = str(v)
    if any(c in s for c in [":", "#", "\"", "'", "\n", "  "]) or s in ("true", "false", "null", "") or (s[0:1].isdigit() and not s.replace(".", "").isdigit()):
        return json.dumps(s, ensure_ascii=False)
    return s


def _fmt_inline_dict(d: dict) -> str:
    parts = []
    for k, v in d.items():
        if v is None:
            continue
        parts.append(f"{k}: {_fmt_scalar(v)}")
    return "{" + ", ".join(parts) + "}"


def format(data: dict) -> str:
    """把 data 序列化成 YAML frontmatter 字符串（含首尾 `---`）。"""
    lines = ["---"]
    order = ["date", "weekday", "sleep", "recovery", "daily", "fitness", "vitals", "weather", "activities"]

    for k in order:
        if k not in data:
            continue
        v = data[k]
        if isinstance(v, dict):
            scalar_only = all(not isinstance(x, (dict, list)) for x in v.values())
            if scalar_only and len(v) <= 8:
                lines.append(f"{k}: {_fmt_inline_dict(v)}")
            else:
                lines.append(f"{k}:")
                for ik, iv in v.items():
                    if iv is None:
                        continue
                    if isinstance(iv, dict):
                        lines.append(f"  {ik}: {_fmt_inline_dict(iv)}")
                    elif isinstance(iv, list):
                        if not iv:
                            lines.append(f"  {ik}: []")
                        else:
                            lines.append(f"  {ik}:")
                            for item in iv:
                                if isinstance(item, dict):
                                    lines.append(f"    - {_fmt_inline_dict(item)}")
                                else:
                                    lines.append(f"    - {_fmt_scalar(item)}")
                    else:
                        lines.append(f"  {ik}: {_fmt_scalar(iv)}")
        elif isinstance(v, list):
            if not v:
                lines.append(f"{k}: []")
            else:
                lines.append(f"{k}:")
                for item in v:
                    if isinstance(item, dict):
                        lines.append(f"  - id: {_fmt_scalar(item.get('id'))}")
                        for ik in ["time", "type", "location", "distance_km", "duration_min",
                                    "moving_duration_min", "avg_hr", "max_hr", "avg_pace", "max_pace",
                                    "elevation_gain_m", "elevation_loss_m", "calories", "cadence_avg",
                                    "cadence_max", "stride_length_cm", "training_effect_aerobic",
                                    "training_effect_anaerobic", "training_load", "vo2max",
                                    "temperature_c", "steps"]:
                            iv = item.get(ik)
                            if iv is not None:
                                lines.append(f"    {ik}: {_fmt_scalar(iv)}")
                        if item.get("hr_zones"):
                            lines.append(f"    hr_zones: {_fmt_inline_dict(item['hr_zones'])}")
                        if item.get("splits"):
                            lines.append(f"    splits:")
                            for lap in item["splits"]:
                                lines.append(f"      - {_fmt_inline_dict(lap)}")
                    else:
                        lines.append(f"  - {_fmt_scalar(item)}")
        else:
            lines.append(f"{k}: {_fmt_scalar(v)}")

    lines.append("---")
    return "\n".join(lines)