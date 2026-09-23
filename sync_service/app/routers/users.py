"""管理员用户管理路由：创建/列表/启停/重置密码。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from app.services import auth, db

router = APIRouter(prefix="/users", tags=["users"])


class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "user"

    @field_validator("role")
    @classmethod
    def _check_role(cls, v: str) -> str:
        if v not in ("admin", "user"):
            raise ValueError("role 必须为 admin 或 user")
        return v


class UserEnabledUpdate(BaseModel):
    enabled: bool


class ResetPasswordRequest(BaseModel):
    new_password: str


@router.get("", summary="用户列表（管理员）")
async def list_users(
    admin: dict[str, Any] = Depends(auth.require_admin),
) -> dict[str, Any]:
    users = db.list_users()
    # 脱敏：不返回 password_hash
    for u in users:
        u.pop("password_hash", None)
    return {"users": users}


@router.post("", summary="创建用户（管理员）")
async def create_user(
    body: UserCreate,
    admin: dict[str, Any] = Depends(auth.require_admin),
) -> dict[str, Any]:
    if len(body.password) < 6:
        raise HTTPException(400, "密码至少 6 位")
    if db.get_user_by_username(body.username):
        raise HTTPException(400, f"用户名已存在: {body.username}")
    u = db.create_user(body.username, auth.hash_password(body.password), body.role)
    db.insert_audit(
        actor=admin["username"], actor_role=admin["role"], action="create_user",
        target=str(u["id"]), detail={"username": body.username, "role": body.role},
    )
    u.pop("password_hash", None)
    return {"user": u}


@router.put("/{user_id}/enabled", summary="启用/禁用用户（管理员）")
async def set_enabled(
    user_id: int,
    body: UserEnabledUpdate,
    admin: dict[str, Any] = Depends(auth.require_admin),
) -> dict[str, Any]:
    target = db.get_user_by_id(user_id)
    if not target:
        raise HTTPException(404, "用户不存在")
    db.update_user_enabled(user_id, body.enabled)
    db.insert_audit(
        actor=admin["username"], actor_role=admin["role"], action="set_user_enabled",
        target=str(user_id), detail={"enabled": body.enabled, "username": target["username"]},
    )
    return {"ok": True, "user_id": user_id, "enabled": body.enabled}


@router.post("/{user_id}/reset-password", summary="重置用户密码（管理员）")
async def reset_password(
    user_id: int,
    body: ResetPasswordRequest,
    admin: dict[str, Any] = Depends(auth.require_admin),
) -> dict[str, Any]:
    target = db.get_user_by_id(user_id)
    if not target:
        raise HTTPException(404, "用户不存在")
    if len(body.new_password) < 6:
        raise HTTPException(400, "新密码至少 6 位")
    db.update_user_password(user_id, auth.hash_password(body.new_password))
    db.insert_audit(
        actor=admin["username"], actor_role=admin["role"], action="reset_pwd",
        target=str(user_id), detail={"username": target["username"]},
    )
    return {"ok": True, "msg": "密码已重置，该用户其他登录将自动失效"}
