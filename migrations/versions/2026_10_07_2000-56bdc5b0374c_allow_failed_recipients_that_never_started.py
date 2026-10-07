"""allow FAILED recipients that never started

A recipient can reach FAILED without being started, for example when its job
could not be queued. Only PROCESSING and SUCCESS rows must have started_at.

Revision ID: 56bdc5b0374c
Revises: da47b265b133
Create Date: 2026-10-07 20:00:00

"""

from collections.abc import Sequence

from alembic import op

revision: str = "56bdc5b0374c"
down_revision: str | None = "da47b265b133"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_NAME = "ck_job_recipients_processed_has_started_at"
OLD_CONDITION = "status NOT IN ('PROCESSING', 'SUCCESS', 'FAILED') OR started_at IS NOT NULL"
NEW_NAME = "ck_job_recipients_processing_or_success_has_started_at"
NEW_CONDITION = "status NOT IN ('PROCESSING', 'SUCCESS') OR started_at IS NOT NULL"


def upgrade() -> None:
    op.drop_constraint(op.f(OLD_NAME), "job_recipients", type_="check")
    op.create_check_constraint(op.f(NEW_NAME), "job_recipients", NEW_CONDITION)


def downgrade() -> None:
    # Fails if FAILED rows without started_at exist; set started_at for them first.
    op.drop_constraint(op.f(NEW_NAME), "job_recipients", type_="check")
    op.create_check_constraint(op.f(OLD_NAME), "job_recipients", OLD_CONDITION)
