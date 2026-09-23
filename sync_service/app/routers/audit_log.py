"""审计日志查询（管理员）。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from app.services import auth, db

router = APIRouter(prefix="/audit-log", tags=["audit-log"])


@router.get("", summary="审计日志（管理员）")
async def list_audit_log(
    action: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    admin: dict[str, Any] = Depends(auth.require_admin),
) -> dict[str, Any]:
    items = db.list_audit_log(action=action, limit=limit, offset=offset)
    return {"items": items, "limit": limit, "offset": offset}
