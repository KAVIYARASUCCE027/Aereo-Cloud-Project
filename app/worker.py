"""Celery application and the one task: process a job.

Start a worker with:  celery -A app.worker:celery_app worker

The message carries only the job ID. All job state lives in PostgreSQL, and
process_job claims a job atomically, so delivering the same message twice
cannot process a job twice. That is what makes the delivery settings below
safe.

Delivery behaviour (Redis broker):
- task_acks_late: a message is acknowledged after the task returns, not when
  it is received. If the worker dies before the task finishes, the message is
  delivered again: immediately if only the child process died
  (task_reject_on_worker_lost), otherwise once Redis's visibility timeout
  expires. A redelivered message for a job that was already claimed returns
  `already_processing` and does nothing; there is no resume of interrupted jobs.
- worker_prefetch_multiplier=1: each worker process reserves one message at a
  time, so queued jobs are not stuck behind a busy process.
- Tasks are never retried automatically; a failed task is logged and acknowledged.
"""

import uuid

from celery import Celery
from celery.signals import worker_process_init

from app.config import get_settings
from app.db import engine
from app.services.generation import process_job

PROCESS_JOB_TASK = "certificates.process_job"

# Seconds an unacknowledged message stays reserved before Redis delivers it
# again. Longer than any expected job; a redelivery while the job still runs is
# harmless (already_processing).
VISIBILITY_TIMEOUT_SECONDS = 3600

celery_app = Celery("certificates", broker=get_settings().celery_broker_url)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    # Job progress lives in PostgreSQL, so Celery task results are never read.
    task_ignore_result=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_transport_options={"visibility_timeout": VISIBILITY_TIMEOUT_SECONDS},
    broker_connection_retry_on_startup=True,
)


@worker_process_init.connect
def _use_fresh_database_connections(**_: object) -> None:
    # A forked worker process must not reuse connections inherited from its parent.
    engine.dispose(close=False)


@celery_app.task(name=PROCESS_JOB_TASK)
def process_job_task(job_id: str) -> dict[str, str]:
    """Thin wrapper around process_job. Exceptions propagate, so Celery logs them."""
    result = process_job(uuid.UUID(job_id))
    # Celery logs this return value; it is not stored anywhere.
    return {"job_id": job_id, "outcome": result.outcome.value, "status": result.status.value}
