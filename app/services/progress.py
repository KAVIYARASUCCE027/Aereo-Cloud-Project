"""Job progress, computed from recipient rows (no separately stored counters)."""

import uuid
from dataclasses import dataclass

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.models import GenerationJob, JobRecipient, JobStatus, RecipientStatus

COUNT_FIELDS = {status: f"{status.value.lower()}_count" for status in RecipientStatus}


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


def load_job_summary(session: Session, job_id: uuid.UUID) -> tuple[GenerationJob, JobProgress]:
    """The job row and its recipient counts, read by one SQL statement.

    One statement sees one snapshot of the database, so the job's status and
    the counts always agree with each other, even while a worker is updating
    rows (separate queries could each see a different moment).
    """
    row = session.execute(_summary_statement(job_id)).one_or_none()
    if row is None:
        raise JobNotFoundError(job_id)
    job = row[0]
    counts = {name: row._mapping[name] for name in COUNT_FIELDS.values()}
    return job, JobProgress(total_count=job.total_count, **counts)


def get_job_progress(session: Session, job_id: uuid.UUID) -> JobProgress:
    return load_job_summary(session, job_id)[1]


def _summary_statement(job_id: uuid.UUID) -> Select:
    counts = [
        func.count(JobRecipient.id).filter(JobRecipient.status == status).label(name)
        for status, name in COUNT_FIELDS.items()
    ]
    return (
        select(GenerationJob, *counts)
        .outerjoin(JobRecipient, JobRecipient.job_id == GenerationJob.id)
        .where(GenerationJob.id == job_id)
        .group_by(GenerationJob.id)
    )


def final_job_status(progress: JobProgress) -> JobStatus:
    """Terminal status from recipient outcomes. Refuses while any entry is unfinished."""
    if progress.unfinished_count or progress.processed_count != progress.total_count:
        raise ValueError(f"not every recipient has a final outcome: {progress}")
    if progress.success_count == progress.total_count:
        return JobStatus.COMPLETED
    if progress.success_count > 0:
        return JobStatus.COMPLETED_WITH_ERRORS
    return JobStatus.FAILED
