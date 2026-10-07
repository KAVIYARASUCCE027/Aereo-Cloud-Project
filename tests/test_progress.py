"""Progress arithmetic and final job status, without a database."""

import pytest

from app.models import JobStatus, RecipientStatus
from app.services.progress import JobProgress, final_job_status


def test_every_recipient_status_has_a_count() -> None:
    fields = set(JobProgress.__dataclass_fields__)

    assert {f"{status.value.lower()}_count" for status in RecipientStatus} <= fields


def test_processed_counts_success_invalid_and_failed() -> None:
    progress = JobProgress(total_count=8, pending_count=2, processing_count=1, success_count=3, invalid_count=1, failed_count=1)

    assert progress.processed_count == 5
    assert progress.unfinished_count == 3
    assert progress.progress_percent == 62.5


def test_progress_reaches_100_when_every_entry_is_final_even_with_failures() -> None:
    progress = JobProgress(total_count=4, success_count=1, invalid_count=1, failed_count=2)

    assert progress.progress_percent == 100


@pytest.mark.parametrize(
    ("progress", "expected"),
    [
        (JobProgress(total_count=3, success_count=3), JobStatus.COMPLETED),
        (JobProgress(total_count=3, success_count=2, failed_count=1), JobStatus.COMPLETED_WITH_ERRORS),
        (JobProgress(total_count=3, success_count=2, invalid_count=1), JobStatus.COMPLETED_WITH_ERRORS),
        (JobProgress(total_count=3, failed_count=2, invalid_count=1), JobStatus.FAILED),
        (JobProgress(total_count=1, failed_count=1), JobStatus.FAILED),
    ],
)
def test_final_status_follows_recipient_outcomes(progress: JobProgress, expected: JobStatus) -> None:
    assert final_job_status(progress) is expected


@pytest.mark.parametrize(
    "progress",
    [
        JobProgress(total_count=3, success_count=2, pending_count=1),
        JobProgress(total_count=3, success_count=2, processing_count=1),
        JobProgress(total_count=3, success_count=2),  # a row is missing entirely
    ],
)
def test_final_status_is_refused_while_any_entry_lacks_an_outcome(progress: JobProgress) -> None:
    with pytest.raises(ValueError, match="final outcome"):
        final_job_status(progress)
