import logging

from fastapi import FastAPI

from app.api.health import router as health_router
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
    return app


app = create_app()
