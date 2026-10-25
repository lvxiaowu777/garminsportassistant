"""业务异常：service 层抛它，HTTP 层统一翻译成状态码。

为什么需要：路由不该写 `if user_id == 0`、`if not ok: raise HTTPException(404)`
这类重复判断。规则在 service 里抛 `AppError`，路由只负责调用；
`server.py` 有一个全局 handler 把它变成 JSONResponse。
"""
from __future__ import annotations


class AppError(Exception):
    """带 HTTP 状态码的业务异常。"""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class NotFoundError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=404)


class AuthRequiredError(AppError):
    def __init__(self, message: str = "请先登录") -> None:
        super().__init__(message, status_code=401)
