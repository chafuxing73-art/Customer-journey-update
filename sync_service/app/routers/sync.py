"""同步与客户管理路由（带鉴权 + 客户 ownership 隔离）。

权限：
- /sync/{code}：登录 + 访问权限（owned/assigned/admin）
- /sync（全量）：仅 admin
- /customers CRUD：登录；改/删需 owner 或 admin
- /customers/{code}/assign：仅 admin
- /customers/{code}/webhook-test：访问权限
- /schedule：仅 admin
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app import config
from app.services import auth, db, qiwai_client, sync_engine

logger = logging.getLogger(__name__)

router = APIRouter()


def _reload_scheduler() -> None:
    from app.main import reload_scheduler
    reload_scheduler()


# ===== 同步触发 =====
@router.post("/sync/{customer_code}", summary="手动触发单客户同步")
async def sync_one(
    customer_code: str,
    user: dict[str, Any] = Depends(auth.get_current_user),
) -> dict[str, Any]:
    # 校验访问权限
    auth.get_customer_if_accessible(customer_code, user)
    db.insert_audit(
        actor=user["username"], actor_role=user["role"],
        action="sync_trigger", target=customer_code,
        detail={"source": "manual"},
    )
    engine = sync_engine.get_engine()
    try:
        result = await engine.sync_customer(
            customer_code, triggered_by=user["username"], trigger_source="manual"
        )
    except sync_engine.SyncInProgressError:
        raise HTTPException(409, f"客户 {customer_code} 正在同步中，请稍后重试")
    return result.to_dict()


@router.post("/sync", summary="同步所有启用客户（仅管理员）")
async def sync_all(
    admin: dict[str, Any] = Depends(auth.require_admin),
) -> dict[str, Any]:
    customers = [c for c in config.list_customers() if c.get("enabled", True)]
    engine = sync_engine.get_engine()
    results = []
    for c in customers:
        try:
            r = await engine.sync_customer(
                c["code"], triggered_by=admin["username"], trigger_source="api_all"
            )
            results.append(r.to_dict())
        except sync_engine.SyncInProgressError:
            results.append({**SyncResult(c["code"]).to_dict(), "skipped_reason": "in_progress"})
    db.insert_audit(
        actor=admin["username"], actor_role="admin",
        action="sync_trigger", target="all",
        detail={"source": "api_all", "count": len(customers)},
    )
    return {"total_customers": len(customers), "results": results}


def SyncResult(code: str):
    # 兼容占位（sync_all 中 in_progress 跳过项）
    class _R:
        def __init__(self, c: str) -> None:
            self.customer_code = c
            self.total = self.new = self.updated = self.skipped = self.failed = 0
            self.errors: list[str] = []

        def to_dict(self) -> dict[str, Any]:
            return {
                "customer_code": self.customer_code, "total": self.total,
                "new": self.new, "updated": self.updated, "skipped": self.skipped,
                "failed": self.failed, "errors": self.errors,
            }
    return _R(code)


# ===== 客户管理 =====
class CustomerCreate(BaseModel):
    code: str
    name: str | None = None
    webhook_url: str
    sync_mode: str = "incremental"
    schedule: str = "0 9,18 * * *"
    enabled: bool = True


class CustomerUpdate(BaseModel):
    webhook_url: str | None = None
    name: str | None = None
    sync_mode: str | None = None
    schedule: str | None = None
    enabled: bool | None = None


class AssignRequest(BaseModel):
    user_id: int


@router.get("/customers", summary="列出我可访问的客户")
async def list_customers(
    user: dict[str, Any] = Depends(auth.get_current_user),
) -> dict[str, Any]:
    if user["role"] == "admin":
        customers = db.list_customers()
    else:
        customers = db.list_customers_for_user(user["id"])
    out = []
    for c in customers:
        c = dict(c)
        c["is_owner"] = c.get("owner_user_id") == user["id"]
        out.append(c)
    return {"customers": out}


@router.get("/customers/{customer_code}", summary="查看单个客户")
async def get_customer(
    customer_code: str,
    user: dict[str, Any] = Depends(auth.get_current_user),
) -> dict[str, Any]:
    cust = auth.get_customer_if_accessible(customer_code, user)
    resp = dict(cust)
    resp["is_owner"] = cust["owner_user_id"] == user["id"]
    resp["assignments"] = db.list_assignments_for_customer(customer_code)
    return resp


@router.post("/customers", summary="新增客户（创建者=owner）")
async def create_customer(
    body: CustomerCreate,
    user: dict[str, Any] = Depends(auth.get_current_user),
) -> dict[str, Any]:
    if db.get_customer(body.code):
        raise HTTPException(400, f"客户已存在: {body.code}")
    cust = db.create_customer(
        code=body.code,
        webhook_url=body.webhook_url,
        owner_user_id=user["id"],
        name=body.name,
        sync_mode=body.sync_mode,
        schedule=body.schedule,
        enabled=body.enabled,
    )
    db.insert_audit(
        actor=user["username"], actor_role=user["role"], action="create_customer",
        target=body.code, detail={"name": body.name},
    )
    _reload_scheduler()
    return cust


@router.put("/customers/{customer_code}", summary="更新客户（owner/admin）")
async def update_customer(
    customer_code: str,
    body: CustomerUpdate,
    user: dict[str, Any] = Depends(auth.get_current_user),
) -> dict[str, Any]:
    auth.require_customer_owner(customer_code, user)
    updates = body.model_dump(exclude_none=True)
    cust = db.update_customer(customer_code, updates)
    db.insert_audit(
        actor=user["username"], actor_role=user["role"], action="update_customer",
        target=customer_code, detail=updates,
    )
    _reload_scheduler()
    return cust or {"code": customer_code}


@router.delete("/customers/{customer_code}", summary="删除客户（owner/admin）")
async def delete_customer(
    customer_code: str,
    user: dict[str, Any] = Depends(auth.get_current_user),
) -> dict[str, Any]:
    auth.require_customer_owner(customer_code, user)
    db.delete_customer(customer_code)
    db.insert_audit(
        actor=user["username"], actor_role=user["role"], action="delete_customer",
        target=customer_code,
    )
    _reload_scheduler()
    return {"code": customer_code, "deleted": True}


# ===== 客户分配（仅管理员） =====
@router.post("/customers/{customer_code}/assign", summary="分配客户给用户（管理员）")
async def assign_customer(
    customer_code: str,
    body: AssignRequest,
    admin: dict[str, Any] = Depends(auth.require_admin),
) -> dict[str, Any]:
    if not db.get_customer(customer_code):
        raise HTTPException(404, f"未找到客户: {customer_code}")
    if not db.get_user_by_id(body.user_id):
        raise HTTPException(404, f"未找到用户: {body.user_id}")
    db.assign_customer(body.user_id, customer_code, assigned_by=admin["id"])
    db.insert_audit(
        actor=admin["username"], actor_role="admin", action="assign_customer",
        target=customer_code, detail={"user_id": body.user_id},
    )
    return {
        "customer_code": customer_code,
        "user_id": body.user_id,
        "assignments": db.list_assignments_for_customer(customer_code),
    }


@router.delete("/customers/{customer_code}/assign/{user_id}", summary="取消分配（管理员）")
async def unassign_customer(
    customer_code: str,
    user_id: int,
    admin: dict[str, Any] = Depends(auth.require_admin),
) -> dict[str, Any]:
    db.unassign_customer(user_id, customer_code)
    db.insert_audit(
        actor=admin["username"], actor_role="admin", action="unassign_customer",
        target=customer_code, detail={"user_id": user_id},
    )
    return {"customer_code": customer_code, "user_id": user_id, "unassigned": True}


@router.get("/customers/{customer_code}/assignments", summary="查看客户被分配给哪些用户")
async def list_assignments(
    customer_code: str,
    user: dict[str, Any] = Depends(auth.get_current_user),
) -> dict[str, Any]:
    auth.get_customer_if_accessible(customer_code, user)
    return {
        "customer_code": customer_code,
        "assignments": db.list_assignments_for_customer(customer_code),
    }


# ===== webhook 测试（零脏数据） =====
@router.post("/customers/{customer_code}/webhook-test", summary="测试 webhook 可用性")
async def webhook_test(
    customer_code: str,
    user: dict[str, Any] = Depends(auth.get_current_user),
) -> dict[str, Any]:
    cust = auth.get_customer_if_accessible(customer_code, user)
    client = qiwai_client.QiwaiClient()
    result = await client.webhook_probe(cust["webhook_url"])
    db.insert_audit(
        actor=user["username"], actor_role=user["role"], action="webhook_test",
        target=customer_code,
        detail={"reachable": result["reachable"], "valid_key": result["valid_key"]},
    )
    return {"customer_code": customer_code, **result}


# ===== 定时任务管理 =====
@router.get("/schedule", summary="查看定时任务状态（管理员）")
async def get_schedule(
    admin: dict[str, Any] = Depends(auth.require_admin),
) -> dict[str, Any]:
    from app.main import scheduler_status
    return scheduler_status()


@router.put("/customers/{customer_code}/schedule", summary="更新客户定时表达式（owner/admin）")
async def update_schedule(
    customer_code: str,
    body: CustomerUpdate,
    user: dict[str, Any] = Depends(auth.get_current_user),
) -> dict[str, Any]:
    auth.require_customer_owner(customer_code, user)
    updates = body.model_dump(exclude_none=True)
    cust = db.update_customer(customer_code, updates)
    db.insert_audit(
        actor=user["username"], actor_role=user["role"], action="update_schedule",
        target=customer_code, detail=updates,
    )
    _reload_scheduler()
    return cust or {"code": customer_code}
