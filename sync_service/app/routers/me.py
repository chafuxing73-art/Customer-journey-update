"""当前用户视角路由：我的客户、我的同步历史。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from app.services import auth, db

router = APIRouter(prefix="/me", tags=["me"])


@router.get("/customers", summary="我能访问的客户（拥有+被分配）")
async def my_customers(
    user: dict[str, Any] = Depends(auth.get_current_user),
) -> dict[str, Any]:
    if user["role"] == "admin":
        customers = db.list_customers()
    else:
        customers = db.list_customers_for_user(user["id"])
    # 标注每个客户的归属关系
    out = []
    for c in customers:
        c = dict(c)
        c["is_owner"] = c.get("owner_user_id") == user["id"]
        out.append(c)
    return {"customers": out}


@router.get("/sync-history", summary="我的同步历史")
async def my_sync_history(
    user: dict[str, Any] = Depends(auth.get_current_user),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    if user["role"] == "admin":
        items = db.list_sync_history(limit=limit, offset=offset)
    else:
        items = db.list_sync_history_for_user(user["id"], limit=limit, offset=offset)
    return {"items": items, "limit": limit, "offset": offset}
