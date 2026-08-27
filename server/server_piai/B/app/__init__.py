# app/__init__.py  (서버 B)
from fastapi import FastAPI

from .core.lifespan import lifespan
from .api.routes_health import router as health_router
from .api.routes_complaints import router as complaints_router
from .api.routes_receive import router as receive_router


def create_app() -> FastAPI:
    app = FastAPI(
        title="Complaint Storage & Platform API (Server B)",
        lifespan=lifespan,
    )

    # Health check (내부/모니터링)
    app.include_router(health_router)

    # 서버 A → 서버 B 내부 연동 API
    app.include_router(receive_router)

    # 플랫폼 조회용 API
    app.include_router(complaints_router)

    return app
