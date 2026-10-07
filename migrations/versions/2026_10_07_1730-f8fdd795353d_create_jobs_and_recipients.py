"""create generation_jobs and job_recipients

Revision ID: f8fdd795353d
Revises:
Create Date: 2026-10-07 17:30:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f8fdd795353d"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Written out instead of imported from app.models: a migration must keep
# describing the schema as it was when the migration was written.
JOB_STATUSES = ("QUEUED", "PROCESSING", "COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED")
RECIPIENT_STATUSES = ("PENDING", "PROCESSING", "SUCCESS", "INVALID", "FAILED")


def _status_in(values: tuple[str, ...]) -> str:
    return "status IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    op.create_table(
        "generation_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_name", sa.String(length=200), nullable=False),
        sa.Column("issuer_name", sa.String(length=200), nullable=False),
        sa.Column("issue_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("total_count", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_generation_jobs")),
        sa.CheckConstraint(_status_in(JOB_STATUSES), name=op.f("ck_generation_jobs_status_valid")),
        sa.CheckConstraint("total_count >= 1", name=op.f("ck_generation_jobs_total_count_positive")),
    )

    op.create_table(
        "job_recipients",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("input_index", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_details", postgresql.JSONB(), nullable=True),
        sa.Column("storage_key", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_recipients")),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["generation_jobs.id"],
            name=op.f("fk_job_recipients_job_id_generation_jobs"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("job_id", "input_index", name=op.f("uq_job_recipients_job_id_input_index")),
        sa.CheckConstraint(_status_in(RECIPIENT_STATUSES), name=op.f("ck_job_recipients_status_valid")),
        sa.CheckConstraint("input_index >= 0", name=op.f("ck_job_recipients_input_index_non_negative")),
        sa.CheckConstraint(
            "(status = 'INVALID' AND name IS NULL AND email IS NULL)"
            " OR (status <> 'INVALID' AND name IS NOT NULL)",
            name=op.f("ck_job_recipients_name_matches_status"),
        ),
        sa.CheckConstraint(
            "status <> 'INVALID' OR error_details IS NOT NULL",
            name=op.f("ck_job_recipients_invalid_has_error_details"),
        ),
    )
    op.create_index("ix_job_recipients_job_id_status", "job_recipients", ["job_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_job_recipients_job_id_status", table_name="job_recipients")
    op.drop_table("job_recipients")
    op.drop_table("generation_jobs")
