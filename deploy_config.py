"""部署凭据加载器 —— 凭据不进版本库。

优先级：环境变量 > 项目根目录的 .deploy.env 文件。

.deploy.env 格式（gitignored）：
    GARMIN_HOST=1.2.3.4
    GARMIN_USER=ubuntu
    GARMIN_PASS=your-password

历史背景：这些值曾经以明文硬编码在 9 个部署脚本里，
2026 年阶段 0 重构时抽出到这里，避免推送到 GitHub 时泄露。
"""
from __future__ import annotations

import os
from pathlib import Path

_ENV_FILE = Path(__file__).resolve().parent / ".deploy.env"

if _ENV_FILE.exists():
    for _line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _key, _, _val = _line.partition("=")
        # 环境变量优先，不覆盖已存在的值
        os.environ.setdefault(_key.strip(), _val.strip())

HOST = os.environ.get("GARMIN_HOST", "")
USER = os.environ.get("GARMIN_USER", "ubuntu")
PASS = os.environ.get("GARMIN_PASS", "")


def require_credentials() -> tuple[str, str, str]:
    """校验凭据齐全，返回 (host, user, pass)。缺失时抛出带指引的异常。"""
    missing = [
        name
        for name, value in (("GARMIN_HOST", HOST), ("GARMIN_PASS", PASS))
        if not value
    ]
    if missing:
        raise SystemExit(
            "缺少部署凭据：" + ", ".join(missing) + "\n"
            "在项目根目录新建 .deploy.env，内容示例：\n"
            "  GARMIN_HOST=你的服务器IP\n"
            "  GARMIN_USER=ubuntu\n"
            "  GARMIN_PASS=你的密码\n"
            "或者用环境变量设置同名变量。"
        )
    return HOST, USER, PASS
