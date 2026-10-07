"""Job progress, computed from recipient rows (no separately stored counters)."""

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import GenerationJob, JobRecipient, JobStatus


class JobNotFoundError(LookupError):
    def __init__(self, job_id: uuid.UUID) -> None:
        super().__init__(f"Job {job_id} does not exist")
        self.job_id = job_id


@dataclass(frozen=True)
class JobProgress:
    total_count: int
    pending_count: int = 0
    processing_count: int = 0
    success_count: int = 0
    invalid_count: int = 0
    failed_count: int = 0

    @property
    def processed_count(self) -> int:
        """Entries with a final outcome. INVALID entries are final from the start."""
        return self.success_count + self.invalid_count + self.failed_count

    @property
    def unfinished_count(self) -> int:
        return self.pending_count + self.processing_count

    @property
    def progress_percent(self) -> float:
        """100 means every entry has a final outcome, not that every certificate succeeded."""
        return self.processed_count / self.total_count * 100


def get_job_progress(session: Session, job_id: uuid.UUID) -> JobProgress:
    total_count = session.scalar(select(GenerationJob.total_count).where(GenerationJob.id == job_id))
    if total_count is None:
        raise JobNotFoundError(job_id)

    rows = session.execute(
        select(JobRecipient.status, func.count())
        .where(JobRecipient.job_id == job_id)
        .group_by(JobRecipient.status)
    )
    counts = {f"{status.value.lower()}_count": count for status, count in rows}
    return JobProgress(total_count=total_count, **counts)


def final_job_status(progress: JobProgress) -> JobStatus:
    """Terminal status from recipient outcomes. Refuses while any entry is unfinished."""
    if progress.unfinished_count or progress.processed_count != progress.total_count:
        raise ValueError(f"not every recipient has a final outcome: {progress}")
    if progress.success_count == progress.total_count:
        return JobStatus.COMPLETED
    if progress.success_count > 0:
        return JobStatus.COMPLETED_WITH_ERRORS
    return JobStatus.FAILED
