"""FastAPI 应用入口与应用工厂。

启动：打开存储、把上次未完成作业标记为 interrupted、启动作业线程池。
关闭：通知取消在跑作业并关闭线程池（不阻塞等待长作业）。
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api.plans_routes import router as plans_router
from .api.routes import router
from .config import settings
from .errors import register_exception_handlers
from .jobs import JobManager
from .plans import PlanManager
from .storage import Storage


def create_app(db_path: str | None = None,
               max_workers: int | None = None) -> FastAPI:
    db_path = db_path or settings.db_path
    max_workers = max_workers or settings.max_workers

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        storage = Storage(db_path)
        jobs = JobManager(storage, max_workers=max_workers)
        plans = PlanManager(storage)
        recovered = jobs.start()
        app.state.storage = storage
        app.state.jobs = jobs
        app.state.plans = plans
        app.state.recovered_on_boot = recovered
        yield
        jobs.shutdown(wait=False)
        storage.close()

    app = FastAPI(
        title="社区服务站精确选址后端",
        version="1.1.0",
        description="平面欧氏距离、统一半径、最少开站数的精确分支定界求解，"
                    "支持版本化存储、后台作业、增量重解与带人口权重的"
                    "分期建设计划。",
        lifespan=lifespan,
    )
    register_exception_handlers(app)
    app.include_router(router)
    app.include_router(plans_router)

    @app.get("/health")
    def health():
        return {"status": "ok", "db": os.path.basename(db_path)}

    return app


app = create_app()
