"""add recipient processing timestamps and outcome rules

Revision ID: da47b265b133
Revises: f8fdd795353d
Create Date: 2026-10-07 19:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "da47b265b133"
down_revision: str | None = "f8fdd795353d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JOB_CHECKS = {
    "ck_generation_jobs_completed_at_matches_status": (
        "(status IN ('COMPLETED', 'COMPLETED_WITH_ERRORS', 'FAILED')) = (completed_at IS NOT NULL)"
    ),
    "ck_generation_jobs_processing_has_started_at": "status <> 'PROCESSING' OR started_at IS NOT NULL",
}

RECIPIENT_CHECKS = {
    "ck_job_recipients_success_has_storage_key": "status <> 'SUCCESS' OR storage_key IS NOT NULL",
    "ck_job_recipients_failed_has_error_details": "status <> 'FAILED' OR error_details IS NOT NULL",
    "ck_job_recipients_processed_has_started_at": (
        "status NOT IN ('PROCESSING', 'SUCCESS', 'FAILED') OR started_at IS NOT NULL"
    ),
    "ck_job_recipients_finished_has_completed_at": (
        "status NOT IN ('SUCCESS', 'FAILED') OR completed_at IS NOT NULL"
    ),
}


def upgrade() -> None:
    # Nullable columns: existing rows (all PENDING or INVALID so far) stay valid.
    op.add_column("job_recipients", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("job_recipients", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))

    for name, condition in JOB_CHECKS.items():
        op.create_check_constraint(op.f(name), "generation_jobs", condition)
    for name, condition in RECIPIENT_CHECKS.items():
        op.create_check_constraint(op.f(name), "job_recipients", condition)


def downgrade() -> None:
    for name in RECIPIENT_CHECKS:
        op.drop_constraint(op.f(name), "job_recipients", type_="check")
    for name in JOB_CHECKS:
        op.drop_constraint(op.f(name), "generation_jobs", type_="check")

    op.drop_column("job_recipients", "completed_at")
    op.drop_column("job_recipients", "started_at")
