"""FastAPI 入口（P0 版本：healthz + TraceLab Viewer）。

启动::

    set PYTHONPATH=src
    .venv\\Scripts\\python -m uvicorn app.main:app --reload

访问 http://127.0.0.1:8000/observe
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI

from app import __version__
from app.config import BASE_DIR, get_settings
from app.core.logging_setup import log_event, setup_logging
from app.core.tracing import init_tracing, shutdown_tracing
from app.observability.viewer.routes import router as observe_router
from app.web.api import business_api, register_errors
from app.web.workspace import Workspace
from app.web.pages import assets, pages


def create_app() -> FastAPI:
    """装配应用。配置 → 日志 → 链路采集 → 路由，顺序不能乱。"""
    settings = get_settings()
    observe_enabled = settings.observe_ui and settings.is_dev
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        settings.ensure_dirs()
        setup_logging(settings.log_level, settings.log_dir)
        init_tracing(settings)
        log_event("app.start", env=settings.env,
                  python=sys.version.split()[0], version=__version__)
        try:
            yield
        finally:
            # 退出前把写队列刷干净，否则最后几条 span 会丢
            log_event("app.stop")
            shutdown_tracing()

    app = FastAPI(
        title="AI 模拟面试系统",
        version=__version__,
        description="本地面试、健康检查与链路观测",
        lifespan=lifespan,
    )

    if observe_enabled:
        app.include_router(observe_router)
    workspace = Workspace(settings.data_dir, BASE_DIR / "data" / "seeds" / "knowledge_questions.json")
    register_errors(app)
    app.include_router(business_api(workspace, settings.observe_db))
    app.include_router(pages(workspace))
    app.mount("/workbench", assets(), name="workbench")

    @app.middleware("http")
    async def prevent_business_caching(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/v1/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        """健康检查：返回版本与配置摘要，不暴露任何密钥。"""
        return {
            "ok": True,
            "data": {
                "status": "healthy",
                "version": __version__,
                "env": settings.env,
                "observe_ui": observe_enabled,
            },
            "error": None,
        }

    return app


app = create_app()
