"""
推送服务 —— FCM Token 管理 + 消息发送 + 训练提醒调度。

依赖：
- firebase-admin（pip install firebase-admin）
- Firebase 服务账号 JSON 文件（从 Firebase Console → 项目设置 → 服务账号 下载）

架构：
  FastAPI 定时任务
    ↓ 检查训练计划 + 健康数据
  push_service.py
    ↓ FCM HTTP v1 API
  Google FCM → 手机通知栏
"""

import sys
import os
import json
import datetime as dt
from pathlib import Path
from typing import Optional

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import firebase_admin
from firebase_admin import credentials, messaging

from database import (
    register_device_token,
    get_active_tokens as db_get_active_tokens,
    remove_token as db_remove_token,
    get_global_day_health,
    get_global_activity_count,
)

# ── Firebase 初始化 ──
_firebase_app: Optional[firebase_admin.App] = None

# 服务账号 JSON 路径（从 Firebase Console 下载）
SERVICE_ACCOUNT_PATH = Path(__file__).resolve().parent.parent / "config" / "firebase-service-account.json"


def init_firebase() -> bool:
    """初始化 Firebase Admin SDK。返回是否成功。"""
    global _firebase_app
    if _firebase_app is not None:
        return True

    if not SERVICE_ACCOUNT_PATH.exists():
        print(f"⚠️ Firebase 服务账号不存在: {SERVICE_ACCOUNT_PATH}")
        print("  请从 Firebase Console → 项目设置 → 服务账号 → 生成新私钥 下载")
        return False

    try:
        cred = credentials.Certificate(str(SERVICE_ACCOUNT_PATH))
        _firebase_app = firebase_admin.initialize_app(cred)
        print("✅ Firebase 推送已就绪")
        return True
    except Exception as e:
        print(f"❌ Firebase 初始化失败: {e}")
        return False


# ── Token 管理 ──

def register_device(token: str, platform: str = "android") -> bool:
    """
    注册设备推送 Token。同一设备多次注册会更新。

    Token 是 FCM 分配给每台手机的唯一标识。
    后端拿这个 Token 调 FCM API，Google 把消息推到对应手机。
    """
    return register_device_token(token, platform)


def get_active_tokens() -> list[str]:
    """获取所有活跃的设备 Token（最近 30 天有活动的）。"""
    return db_get_active_tokens()


def remove_token(token: str):
    """移除失效的 Token。"""
    db_remove_token(token)


# ── 发送推送 ──

def send_push(
    token: str,
    title: str,
    body: str,
    click_action: str = "dashboard",
    data: dict | None = None,
) -> bool:
    """
    通过 FCM 发送推送通知到单台设备。

    参数：
        token         : FCM 设备 Token
        title         : 通知标题
        body          : 通知内容
        click_action  : 点击后跳转到哪个 tab（dashboard/chat/history）
        data          : 附加数据
    """
    if _firebase_app is None:
        print("⚠️ Firebase 未初始化，推送未发送")
        return False

    payload_data = {"click_action": click_action}
    if data:
        payload_data.update(data)

    message = messaging.Message(
        token=token,
        notification=messaging.Notification(
            title=title,
            body=body,
        ),
        data=payload_data,
        android=messaging.AndroidConfig(
            priority="normal",
            notification=messaging.AndroidNotification(
                channel_id="training_reminder",
                color="#3B82F6",
            ),
        ),
    )

    try:
        response = messaging.send(message)
        print(f"✅ 推送成功: {response} → {title}")
        return True
    except messaging.UnregisteredError:
        print(f"⚠️ Token 已失效，移除: {token[:20]}...")
        remove_token(token)
        return False
    except Exception as e:
        print(f"❌ 推送失败: {e}")
        return False


def send_broadcast(
    title: str,
    body: str,
    click_action: str = "dashboard",
) -> int:
    """向所有活跃设备发送推送。返回成功发送数量。"""
    tokens = get_active_tokens()
    success = 0
    for token in tokens:
        if send_push(token, title, body, click_action):
            success += 1
    return success


# ── 训练提醒调度 ──

def check_and_notify() -> dict:
    """
    定时任务：检查该不该发训练提醒。由 server.py 的 scheduler 调用。

    规则：
    - 检查时间：早 8:00 + 晚 19:00
    - 如果昨天没有运动 → 发"今天要不要跑？"
    - 如果今天预计有长跑 → 发"今天有长跑计划"
    - 如果连续休息 3 天 → 发"该动一动了"
    - 如果 HRV 低 → 发"今天注意恢复"

    返回 {"sent": 数量, "reason": "..."}
    """
    if _firebase_app is None:
        if not init_firebase():
            return {"sent": 0, "reason": "Firebase 未初始化"}

    tokens = get_active_tokens()
    if not tokens:
        return {"sent": 0, "reason": "无活跃设备"}

    now = dt.datetime.now()
    hour = now.hour

    # 只在早晚检查
    is_morning = 7 <= hour <= 9
    is_evening = 18 <= hour <= 20
    if not is_morning and not is_evening:
        return {"sent": 0, "reason": f"不在推送时段（当前 {hour} 点）"}

    # 查昨天的数据
    yesterday = (now - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    row = get_global_day_health(yesterday)

    # 查昨天有没有运动
    acts_cnt = get_global_activity_count(yesterday)

    # 决定发什么
    title = "Garmin Coach"
    body = ""
    click_action = "dashboard"

    if row is None:
        body = "今天还没同步数据，打开看看吧 👋"
    elif acts_cnt == 0:
        if is_morning:
            body = "昨天休息了一天，今天状态怎么样？要不要跑一跑？"
        else:
            body = "今天还没有运动记录，晚上还跑吗？"
    elif row["hrv"] and row["hrv"] < 50:
        body = f"昨晚 HRV {row['hrv']} 偏低，今天注意恢复，别冲太猛 💪"
        click_action = "chat"
    else:
        if is_evening:
            body = "今天的数据已同步，来看看今天的恢复状态 📊"
        else:
            return {"sent": 0, "reason": "不需要提醒"}

    # 发送
    success = 0
    for token in tokens:
        if send_push(token, title, body, click_action):
            success += 1

    return {"sent": success, "reason": body}
