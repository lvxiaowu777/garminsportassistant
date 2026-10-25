"""手表设备的业务规则（绑定 / MFA / 解绑 / 开关 / 单选 / 列表）。

以前这些判断写在路由里：
- 品牌白名单
- Garmin 需要账号密码、可能需要两步验证
- 解绑/开关/切换找不到绑定要 404
- 绑定成功后后台触发首次同步

现在收在这里，路由只负责鉴权 + 调这些函数。规则出错抛 ``AppError``，
由 ``server.py`` 的全局 handler 翻译成 HTTP 状态码。
"""
from __future__ import annotations

import logging
import threading

from garmin_coach.db.database import (
    bind_watch,
    get_watch_bindings,
    select_watch,
    set_watch_enabled,
    unbind_watch,
)
from garmin_coach.errors import AppError, NotFoundError
from garmin_coach.integrations.garmin_sync import (
    complete_mfa,
    login_and_store,
    sync_user_garmin,
)

log = logging.getLogger(__name__)

SUPPORTED_BRANDS = ("garmin", "apple", "huawei", "xiaomi")


def _check_brand(brand: str) -> None:
    if brand not in SUPPORTED_BRANDS:
        raise AppError(f"不支持的手表品牌: {brand}", 400)


def _start_first_sync(user_id: int, days: int = 7) -> None:
    """绑定成功后后台拉一次历史（不阻塞绑定请求）。"""
    threading.Thread(
        target=sync_user_garmin, args=(user_id, days), daemon=True
    ).start()


def list_bindings(user_id: int) -> list[dict]:
    return get_watch_bindings(user_id)


def bind(user_id: int, brand: str, account: str, password: str) -> dict:
    """绑定设备。Garmin 走官方登录（可能触发两步验证）。"""
    _check_brand(brand)

    if brand == "garmin":
        if not account or not password:
            raise AppError("Garmin 需要邮箱和密码", 400)
        needs_mfa, err = login_and_store(user_id, account, password)
        if err:
            raise AppError(f"登录失败: {err}", 400)
        if needs_mfa:
            return {"ok": True, "needs_mfa": True, "message": "需要两步验证码"}
        bind_watch(user_id, brand, account, auth_token="")
        _start_first_sync(user_id)
        return {"ok": True, "needs_mfa": False, "message": f"已绑定 {brand}"}

    # 其他品牌：暂只记录账号（多品牌预留）
    if not bind_watch(user_id, brand, account, auth_token=password):
        raise AppError("绑定失败", 500)
    return {"ok": True, "needs_mfa": False, "message": f"已绑定 {brand}"}


def bind_mfa(user_id: int, code: str) -> dict:
    """两步验证第二步。"""
    if not (code or "").strip():
        raise AppError("验证码不能为空", 400)
    ok, err, account = complete_mfa(user_id, code.strip())
    if not ok:
        raise AppError(err or "验证失败", 400)
    if account:
        bind_watch(user_id, "garmin", account, auth_token="")
    _start_first_sync(user_id)
    return {"ok": True, "needs_mfa": False, "message": "已绑定 Garmin"}


def unbind(user_id: int, brand: str) -> dict:
    if not unbind_watch(user_id, brand):
        raise NotFoundError(f"未找到 {brand} 绑定")
    return {"ok": True, "message": f"已解绑 {brand}"}


def toggle(user_id: int, brand: str, enabled: bool) -> dict:
    """显示 / 关闭设备。关闭后同步会跳过该品牌（同步侧读同一个开关）。"""
    _check_brand(brand)
    if not set_watch_enabled(user_id, brand, enabled):
        raise NotFoundError(f"未找到 {brand} 绑定")
    log.info("watch toggled user=%s brand=%s enabled=%s", user_id, brand, enabled)
    return {
        "ok": True,
        "enabled": enabled,
        "message": f"{brand} 已{'显示' if enabled else '关闭'}",
    }


def select(user_id: int, brand: str) -> dict:
    """单选设备：只启用一个品牌，其他关闭。"""
    _check_brand(brand)
    if not select_watch(user_id, brand):
        raise NotFoundError(f"未找到 {brand} 绑定")
    return {"ok": True, "active_brand": brand}
