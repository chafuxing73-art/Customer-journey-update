"""同步历史全量查询（管理员）+ 按客户查询（有访问权限者）。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.services import auth, db

router = APIRouter(prefix="/sync-history", tags=["sync-history"])


@router.get("", summary="同步历史（管理员看全部；按客户查询需访问权限）")
async def list_sync_history(
    customer_code: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: dict[str, Any] = Depends(auth.get_current_user),
) -> dict[str, Any]:
    if customer_code:
        # 指定客户：admin 直通；普通用户校验权限
        if user["role"] != "admin" and not db.is_customer_accessible(
            customer_code, user["id"], user["role"]
        ):
            raise HTTPException(403, "无权查看该客户的同步历史")
        items = db.list_sync_history(customer_code=customer_code, limit=limit, offset=offset)
    elif user["role"] == "admin":
        items = db.list_sync_history(limit=limit, offset=offset)
    else:
        items = db.list_sync_history_for_user(user["id"], limit=limit, offset=offset)
    return {"items": items, "limit": limit, "offset": offset}
