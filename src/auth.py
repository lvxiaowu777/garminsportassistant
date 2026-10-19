"""
JWT 鉴权模块 —— 密码哈希、Token 签发、鉴权依赖。
纯标准库 + PyJWT，无额外依赖。
"""
import hashlib
import os
import datetime as dt
from typing import Optional
from pathlib import Path

import jwt
from fastapi import HTTPException, Request, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

# ── 配置 ──
SECRET_KEY = os.environ.get("JWT_SECRET", "garmin-coach-dev-secret-key-change-in-prod")
JWT_ALGORITHM = "HS256"
# Token 不过期（2026-08-20 用户要求：登录一次长期有效）。如果以后要改回过期，
# 在 create_token 里加回 "exp" 字段。

# ── 密码哈希 ──

def hash_password(password: str) -> str:
    """PBKDF2-SHA256 哈希密码，返回 salt$hash 格式。"""
    salt = os.urandom(32)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return salt.hex() + "$" + key.hex()


def verify_password(password: str, stored: str) -> bool:
    """验证密码是否匹配。"""
    try:
        salt_hex, key_hex = stored.split("$")
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(key_hex)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
        return actual == expected
    except Exception:
        return False


# ── JWT ──

def create_token(user_id: int) -> str:
    """签发 JWT（不过期）。"""
    payload = {
        "user_id": user_id,
        "iat": dt.datetime.now(dt.timezone.utc),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> Optional[int]:
    """解码 JWT，返回 user_id；无效返回 None。"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[JWT_ALGORITHM])
        return payload.get("user_id")
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


# ── FastAPI 鉴权依赖 ──

security = HTTPBearer(auto_error=False)


async def get_current_user(request: Request, credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)) -> int:
    """从 Authorization: Bearer <token> 解出 user_id。
    如果没带 token，返回 0（表示未登录，向后兼容单用户模式）。
    """
    if credentials is None:
        return 0  # 未登录，向后兼容
    user_id = decode_token(credentials.credentials)
    if user_id is None:
        raise HTTPException(401, "Token 无效或已过期，请重新登录")
    return user_id
