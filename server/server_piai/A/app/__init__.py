# app/__init__.py  (서버 A)
from fastapi import FastAPI

from .core.lifespan import lifespan
from .api.routes_health import router as health_router
from .api.routes_upload_audio import router as upload_router
from .api.routes_live_progress import router as live_progress_router


def create_app() -> FastAPI:
    app = FastAPI(
        title="AI Processing Server (Server A) - saturi STT correction pipeline",
        lifespan=lifespan,
    )

    # 헬스체크 (내부 모니터링)
    app.include_router(health_router)

    # 녹취 서버 → 오디오 업로드 API
    app.include_router(upload_router)

    # 진행상황 JSON (읽기 전용). 루프백에서만 응답 → SSH 포워딩으로 UI 를 원격에서 돌릴 때 쓴다.
    app.include_router(live_progress_router)

    return app
