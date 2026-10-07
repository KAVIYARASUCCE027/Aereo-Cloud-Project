"""Celery application.

Phase 1 only configures the app so the worker container can start and
connect to Redis. The job-processing task is added in Phase 5.
"""

import logging

from celery import Celery

from app.config import get_settings

settings = get_settings()
logging.basicConfig(level=settings.log_level)

celery_app = Celery("certificates", broker=settings.celery_broker_url)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    # Job progress lives in PostgreSQL, so Celery task results are never read.
    task_ignore_result=True,
    broker_connection_retry_on_startup=True,
)
