"""鉴权层：密码哈希、JWT 签发/校验、FastAPI Depends 注入、客户 ownership 校验。

策略：单 access token（HS256），有效期由 JWT_EXPIRE_DAYS 控制。
改密/重置后更新 users.pwd_changed_at，校验 token 时若 iat < pwd_changed_at 即拒绝，
实现改密即全端踢出，无需 refresh token。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

from app import config
from app.services import db

logger = logging.getLogger(__name__)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)


def _jwt_secret() -> str:
    secret = os.getenv("JWT_SECRET", "").strip()
    if not secret or secret == "please-change-this-to-a-random-32-byte-string":
        # 启动期已校验过，这里兜底
        raise RuntimeError("JWT_SECRET 未配置或仍为占位符，请修改 .env")
    return secret


def _jwt_expire_days() -> int:
    try:
        return int(os.getenv("JWT_EXPIRE_DAYS", "7"))
    except ValueError:
        return 7


# ===== 密码哈希（直接用 bcrypt，兼容 Python 3.14 + bcrypt 5.x）=====
def hash_password(password: str) -> str:
    # bcrypt 限制 72 字节，超长截断（与原 passlib 行为一致）
    pw_bytes = password.encode("utf-8")[:72]
    return bcrypt.hashpw(pw_bytes, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8")[:72], hashed.encode("utf-8"))
    except Exception:  # noqa: BLE001
        return False


def authenticate(username: str, password: str) -> dict[str, Any] | None:
    """用户名+密码校验，返回用户 dict 或 None。"""
    u = db.get_user_by_username(username)
    if not u or not u.get("enabled"):
        return None
    if not verify_password(password, u["password_hash"]):
        return None
    return u


# ===== JWT =====
def create_access_token(user: dict[str, Any]) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user["id"]),
        "username": user["username"],
        "role": user.get("role", "user"),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=_jwt_expire_days())).timestamp()),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm="HS256")


def decode_token(token: str) -> dict[str, Any]:
    """解码并校验签名/有效期/改密失效。失败抛 HTTPException 401。"""
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token 已过期")
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token 无效")

    user_id = int(payload.get("sub", 0))
    u = db.get_user_by_id(user_id)
    if not u or not u.get("enabled"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在或已禁用")

    # 改密失效：token 签发时间早于 pwd_changed_at 则拒绝
    pwd_changed = u.get("pwd_changed_at")
    if pwd_changed:
        try:
            pwd_changed_ts = int(
                datetime.fromisoformat(pwd_changed).timestamp()
            )
            if int(payload.get("iat", 0)) < pwd_changed_ts:
                raise HTTPException(
                    status.HTTP_401_UNAUTHORIZED, "密码已变更，请重新登录"
                )
        except ValueError:
            pass

    # 同步 role（管理员被降级等场景）
    payload["role"] = u["role"]
    payload["username"] = u["username"]
    return payload


# ===== Depends 注入 =====
async def get_current_user(
    token: str | None = Depends(oauth2_scheme),
) -> dict[str, Any]:
    """登录校验。返回 {id, username, role, ...}。"""
    if not token:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "未提供认证 token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_token(token)
    u = db.get_user_by_id(int(payload["sub"]))
    if not u:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在")
    return u


async def require_admin(
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """要求管理员角色。"""
    if user.get("role") != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要管理员权限")
    return user


def get_customer_if_accessible(
    customer_code: str, user: dict[str, Any]
) -> dict[str, Any]:
    """取客户配置并校验访问权限。admin 直通；user 需 owner 或被分配。

    无权限抛 403；客户不存在抛 404。
    """
    cust = db.get_customer(customer_code)
    if not cust:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"未找到客户: {customer_code}")
    if not db.is_customer_accessible(customer_code, user["id"], user.get("role", "user")):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "无权访问该客户"
        )
    return cust


def require_customer_owner(
    customer_code: str, user: dict[str, Any]
) -> dict[str, Any]:
    """仅 owner 或 admin 可通过（用于改/删客户）。"""
    cust = db.get_customer(customer_code)
    if not cust:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"未找到客户: {customer_code}")
    if user.get("role") == "admin":
        return cust
    if cust["owner_user_id"] != user["id"]:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "仅客户创建者或管理员可操作"
        )
    return cust
