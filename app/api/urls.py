"""URL paths returned in API responses, kept in one place so they match the routes."""

import uuid

JOBS_PREFIX = "/api/v1/jobs"
CERTIFICATES_PREFIX = "/api/v1/certificates"


def job_status_url(job_id: uuid.UUID) -> str:
    return f"{JOBS_PREFIX}/{job_id}"


def job_recipients_url(job_id: uuid.UUID) -> str:
    return f"{JOBS_PREFIX}/{job_id}/recipients"


def certificate_download_url(certificate_id: uuid.UUID) -> str:
    return f"{CERTIFICATES_PREFIX}/{certificate_id}/download"
