"""Hand-off point between job creation and background processing.

TEMPORARY (Phase 2): background processing is not connected. Jobs are saved
as QUEUED and stay QUEUED; nothing is published to Redis. Phase 5 replaces the
body of enqueue_job with publishing the Celery task, keeping this signature.
"""

import logging
import uuid

logger = logging.getLogger(__name__)


def enqueue_job(job_id: uuid.UUID) -> None:
    logger.warning(
        "Job %s saved as QUEUED but not dispatched: background processing is not connected yet (Phase 5)",
        job_id,
    )
