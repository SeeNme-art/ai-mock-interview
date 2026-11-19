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
from fastapi.responses import RedirectResponse

from app import __version__
from app.config import get_settings
from app.core.logging_setup import log_event, setup_logging
from app.core.tracing import init_tracing, shutdown_tracing
from app.observability.viewer.routes import router as observe_router


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
        description="P0：健康检查与本地链路观测；面试业务待后续阶段实现",
        lifespan=lifespan,
    )

    if observe_enabled:
        app.include_router(observe_router)

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

    @app.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        return RedirectResponse(url="/observe" if observe_enabled else "/healthz")

    return app


app = create_app()
