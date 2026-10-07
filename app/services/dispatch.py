"""Hand-off from job creation to background processing: one Celery task per job.

enqueue_job publishes the task `certificates.process_job` with the job ID as
its only argument. It is called only after the job has been committed, so a
worker can never receive the ID of a job it cannot read.

Not covered (documented in docs/design.md): if the process dies after the
commit and before the publish, the job stays QUEUED with no message. There is
no transactional outbox or sweeper yet.
"""

import logging
import uuid

from app.worker import process_job_task

logger = logging.getLogger(__name__)

# One retry: enough to replace a pooled connection that went stale when Redis
# restarted, without holding the HTTP request long during an outage. Measured
# in Docker Compose: a refused connection fails in under a second, but while the
# redis container is stopped each lookup of its name takes 4-8 s, so the request
# waits correspondingly longer before the 503.
PUBLISH_RETRY_POLICY = {"max_retries": 1, "interval_start": 0, "interval_step": 0.5, "interval_max": 0.5}


class DispatchError(RuntimeError):
    def __init__(self, job_id: uuid.UUID) -> None:
        super().__init__(f"Job {job_id} could not be queued for processing")
        self.job_id = job_id


def enqueue_job(job_id: uuid.UUID) -> None:
    """Publish the processing task. Raises DispatchError if publishing fails."""
    try:
        process_job_task.apply_async(args=[str(job_id)], retry=True, retry_policy=PUBLISH_RETRY_POLICY)
    except Exception as exc:  # kombu OperationalError for broker outages, but never swallowed
        logger.exception("Could not publish job %s to the queue", job_id)
        raise DispatchError(job_id) from exc
