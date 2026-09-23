"""FastAPI 入口 + APScheduler 定时任务调度。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app import config
from app.routers import audit_log as audit_log_router
from app.routers import auth as auth_router
from app.routers import me as me_router
from app.routers import sync as sync_router
from app.routers import sync_history as sync_history_router
from app.routers import users as users_router
from app.services import db, sync_engine

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ===== 全局调度器 =====
_scheduler: AsyncIOScheduler | None = None


def _build_jobs() -> list[dict[str, Any]]:
    """从 customers.yaml 构建定时任务列表。"""
    jobs: list[dict[str, Any]] = []
    for c in config.list_customers():
        if not c.get("enabled", True):
            continue
        cron = c.get("schedule") or ""
        if not cron:
            continue
        jobs.append({
            "id": f"sync_{c['code']}",
            "customer_code": c["code"],
            "name": c.get("name", c["code"]),
            "cron": cron,
        })
    return jobs


async def _run_customer_sync(customer_code: str) -> None:
    """定时任务执行的同步函数。"""
    try:
        engine = sync_engine.get_engine()
        await engine.sync_customer(
            customer_code, triggered_by="system", trigger_source="scheduled"
        )
    except sync_engine.SyncInProgressError:
        logger.info("定时同步跳过 %s：已有同步进行中", customer_code)
    except Exception:  # noqa: BLE001
        logger.exception("定时同步异常 customer=%s", customer_code)


def reload_scheduler() -> None:
    """重新装载调度器（客户配置变更后调用）。"""
    global _scheduler
    if _scheduler is None:
        return
    # 清空旧任务
    for job in list(_scheduler.get_jobs()):
        job.remove()
    # 重新添加
    for j in _build_jobs():
        try:
            trigger = CronTrigger.from_crontab(j["cron"])
        except Exception as e:  # noqa: BLE001
            logger.warning("客户 %s cron 表达式无效 %r: %s", j["customer_code"], j["cron"], e)
            continue
        _scheduler.add_job(
            _run_customer_sync,
            trigger=trigger,
            id=j["id"],
            args=[j["customer_code"]],
            name=j["name"],
            replace_existing=True,
        )
        logger.info("已注册定时任务 %s: %s (%s)", j["id"], j["name"], j["cron"])


def scheduler_status() -> dict[str, Any]:
    if _scheduler is None:
        return {"running": False, "jobs": []}
    jobs = [
        {
            "id": j.id,
            "name": j.name,
            "next_run_time": str(j.next_run_time) if j.next_run_time else None,
        }
        for j in _scheduler.get_jobs()
    ]
    return {"running": _scheduler.running, "jobs": jobs}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动：建表 + bootstrap admin + 迁移种子
    db.init_all()
    # 启动调度器
    global _scheduler
    _scheduler = AsyncIOScheduler()
    _scheduler.start()
    reload_scheduler()
    logger.info("调度器已启动，已加载 %d 个定时任务", len(_build_jobs()))
    yield
    # 关闭
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        logger.info("调度器已关闭")


app = FastAPI(
    title="客户轨迹自动更新同步服务",
    description="从锦联 ERP 拉取运单+轨迹数据，写入企微智能表格",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(sync_router.router, tags=["sync"])
app.include_router(auth_router.router)
app.include_router(users_router.router)
app.include_router(me_router.router)
app.include_router(sync_history_router.router)
app.include_router(audit_log_router.router)

# ===== 静态文件 + 前端页面 =====
_STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse, summary="前端页面")
async def index() -> HTMLResponse:
    html_path = _STATIC_DIR / "index.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/health", summary="健康检查")
async def health() -> dict[str, str]:
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )
