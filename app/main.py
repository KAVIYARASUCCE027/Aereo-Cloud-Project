import logging

from fastapi import FastAPI

from app.api.certificates import router as certificates_router
from app.api.health import router as health_router
from app.api.jobs import router as jobs_router
from app.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)

    app = FastAPI(
        title="Bulk Certificate Generator",
        version="0.1.0",
        description="Submit a batch of recipients and generate one PDF certificate per recipient.",
    )
    app.include_router(health_router)
    app.include_router(jobs_router)
    app.include_router(certificates_router)
    return app


app = create_app()
