"""鉴权路由：登录、当前用户信息、改密。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel

from app.services import auth, db

router = APIRouter(prefix="/auth", tags=["auth"])


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


@router.post("/login", summary="登录获取 token")
async def login(form: OAuth2PasswordRequestForm = Depends()) -> dict[str, Any]:
    user = auth.authenticate(form.username, form.password)
    if not user:
        raise _login_error()
    token = auth.create_access_token(user)
    db.update_last_login(user["id"])
    db.insert_audit(
        actor=user["username"], actor_role=user["role"], action="login",
        target=str(user["id"]),
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "role": user["role"],
        "username": user["username"],
    }


@router.get("/me", summary="当前用户信息")
async def me(user: dict[str, Any] = Depends(auth.get_current_user)) -> dict[str, Any]:
    return {
        "id": user["id"],
        "username": user["username"],
        "role": user["role"],
        "enabled": bool(user.get("enabled", 1)),
    }


@router.post("/change-password", summary="修改自己的密码")
async def change_password(
    body: ChangePasswordRequest,
    user: dict[str, Any] = Depends(auth.get_current_user),
) -> dict[str, Any]:
    if not auth.verify_password(body.old_password, user["password_hash"]):
        raise _login_error("旧密码不正确")
    if len(body.new_password) < 6:
        from fastapi import HTTPException
        raise HTTPException(400, "新密码至少 6 位")
    db.update_user_password(user["id"], auth.hash_password(body.new_password))
    db.insert_audit(
        actor=user["username"], actor_role=user["role"], action="change_pwd",
        target=str(user["id"]),
    )
    return {"ok": True, "msg": "密码已修改，其他登录将自动失效"}


def _login_error(detail: str = "用户名或密码错误"):
    from fastapi import HTTPException, status
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, detail,
        headers={"WWW-Authenticate": "Bearer"},
    )
