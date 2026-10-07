"""Read side: paginated recipient results and certificate files.

Job status and counts come from app/services/progress.py (load_job_summary).
"""

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import GenerationJob, JobRecipient, RecipientStatus
from app.services.progress import JobNotFoundError
from app.services.storage import InvalidStorageKeyError, resolve_storage_path

logger = logging.getLogger(__name__)

MAX_PAGE_SIZE = 200


class CertificateNotFoundError(LookupError):
    def __init__(self, certificate_id: uuid.UUID) -> None:
        super().__init__(f"Certificate {certificate_id} does not exist")


class CertificateNotAvailableError(Exception):
    """The recipient exists but has no successful certificate (yet)."""

    def __init__(self, certificate_id: uuid.UUID, status: RecipientStatus) -> None:
        super().__init__(f"Certificate {certificate_id} is {status}")
        self.status = status

    @property
    def may_become_available(self) -> bool:
        return self.status in (RecipientStatus.PENDING, RecipientStatus.PROCESSING)


class CertificateFileMissingError(Exception):
    """The database says SUCCESS, but the stored file cannot be read."""


@dataclass(frozen=True)
class RecipientPage:
    total: int
    items: list[JobRecipient]


@dataclass(frozen=True)
class CertificateFile:
    content: bytes
    filename: str


def list_recipients(session: Session, job_id: uuid.UUID, *, limit: int, offset: int) -> RecipientPage:
    """One page of a job's recipients in submission (input_index) order."""
    total = session.scalar(select(GenerationJob.total_count).where(GenerationJob.id == job_id))
    if total is None:
        raise JobNotFoundError(job_id)
    query = (
        select(JobRecipient)
        .where(JobRecipient.job_id == job_id)
        .order_by(JobRecipient.input_index)
        .limit(limit)
        .offset(offset)
    )
    return RecipientPage(total=total, items=list(session.scalars(query)))


def read_certificate(session: Session, certificate_id: uuid.UUID, storage_root: Path) -> CertificateFile:
    """The PDF of a SUCCESS recipient.

    The database decides availability; a file on disk alone never makes a
    certificate downloadable. The path comes from the stored key and must
    resolve inside storage_root. The file is read into memory (about 65 KB),
    so it cannot disappear halfway through the response.
    """
    row = session.execute(
        select(JobRecipient.status, JobRecipient.storage_key).where(JobRecipient.id == certificate_id)
    ).one_or_none()
    if row is None:
        raise CertificateNotFoundError(certificate_id)
    if row.status is not RecipientStatus.SUCCESS:
        raise CertificateNotAvailableError(certificate_id, row.status)

    try:
        content = resolve_storage_path(storage_root, row.storage_key).read_bytes()
    except (InvalidStorageKeyError, OSError) as exc:
        logger.error("Certificate %s is SUCCESS but its file cannot be read: %s", certificate_id, exc)
        raise CertificateFileMissingError(f"Certificate {certificate_id} file is missing") from exc
    return CertificateFile(content=content, filename=f"certificate-{certificate_id}.pdf")
