"""SQLAlchemy models.

The schema itself is created by Alembic migrations (`migrations/versions/`),
never by `create_all()` at startup. Status columns are VARCHAR plus a CHECK
constraint rather than native PostgreSQL ENUM types, so adding a status later
is a simple constraint change.
"""

import enum
import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Deterministic constraint names keep Alembic migrations stable and readable.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class JobStatus(enum.StrEnum):
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    FAILED = "FAILED"


class RecipientStatus(enum.StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    SUCCESS = "SUCCESS"
    INVALID = "INVALID"
    FAILED = "FAILED"


STATUS_LENGTH = 32


def _status_type(enum_class: type[enum.StrEnum]) -> Enum:
    # Stored as VARCHAR; the CHECK constraint below is what the database enforces.
    return Enum(
        enum_class,
        native_enum=False,
        create_constraint=False,
        length=STATUS_LENGTH,
        validate_strings=True,
    )


def _status_in(enum_class: type[enum.StrEnum]) -> str:
    allowed = ", ".join(f"'{member.value}'" for member in enum_class)
    return f"status IN ({allowed})"


class GenerationJob(Base):
    __tablename__ = "generation_jobs"
    __table_args__ = (
        CheckConstraint(_status_in(JobStatus), name="status_valid"),
        CheckConstraint("total_count >= 1", name="total_count_positive"),
        # completed_at is set exactly when the job has reached a terminal status.
        CheckConstraint(
            "(status IN ('COMPLETED', 'COMPLETED_WITH_ERRORS', 'FAILED')) = (completed_at IS NOT NULL)",
            name="completed_at_matches_status",
        ),
        CheckConstraint("status <> 'PROCESSING' OR started_at IS NOT NULL", name="processing_has_started_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    event_name: Mapped[str] = mapped_column(String(200))
    issuer_name: Mapped[str] = mapped_column(String(200))
    issue_date: Mapped[date] = mapped_column(Date)
    status: Mapped[JobStatus] = mapped_column(_status_type(JobStatus), default=JobStatus.QUEUED)
    # Number of submitted entries, valid or not. Per-status counts are computed
    # from job_recipients rather than stored, so they cannot drift.
    total_count: Mapped[int] = mapped_column(Integer)

    # Set only when the job as a whole fails (for example, an infrastructure error).
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    recipients: Mapped[list["JobRecipient"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="JobRecipient.input_index",
    )


class JobRecipient(Base):
    """One submitted recipient entry. A SUCCESS row's id is also its certificate ID."""

    __tablename__ = "job_recipients"
    __table_args__ = (
        UniqueConstraint("job_id", "input_index"),
        Index("ix_job_recipients_job_id_status", "job_id", "status"),
        CheckConstraint(_status_in(RecipientStatus), name="status_valid"),
        CheckConstraint("input_index >= 0", name="input_index_non_negative"),
        # INVALID rows keep no submitted values; they are identified by
        # input_index and explained by error_details. Every other row has a
        # validated name.
        CheckConstraint(
            "(status = 'INVALID' AND name IS NULL AND email IS NULL)"
            " OR (status <> 'INVALID' AND name IS NOT NULL)",
            name="name_matches_status",
        ),
        CheckConstraint("status <> 'INVALID' OR achievement IS NULL", name="invalid_has_no_achievement"),
        CheckConstraint(
            "status <> 'INVALID' OR error_details IS NOT NULL",
            name="invalid_has_error_details",
        ),
        # SUCCESS means the PDF was saved; FAILED always explains why.
        CheckConstraint("status <> 'SUCCESS' OR storage_key IS NOT NULL", name="success_has_storage_key"),
        CheckConstraint("status <> 'FAILED' OR error_details IS NOT NULL", name="failed_has_error_details"),
        # FAILED does not require started_at: a recipient can fail without ever
        # starting, e.g. when its job could not be queued.
        CheckConstraint(
            "status NOT IN ('PROCESSING', 'SUCCESS') OR started_at IS NOT NULL",
            name="processing_or_success_has_started_at",
        ),
        CheckConstraint(
            "status NOT IN ('SUCCESS', 'FAILED') OR completed_at IS NOT NULL",
            name="finished_has_completed_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("generation_jobs.id", ondelete="CASCADE")
    )
    # Zero-based position in the submitted recipients list.
    input_index: Mapped[int] = mapped_column(Integer)
    name: Mapped[str | None] = mapped_column(String(150))
    email: Mapped[str | None] = mapped_column(String(320))
    # Optional validated text printed on the certificate; NULL when not given.
    achievement: Mapped[str | None] = mapped_column(String(150))
    status: Mapped[RecipientStatus] = mapped_column(_status_type(RecipientStatus))
    # List of {"field", "code", "message"} objects for INVALID and FAILED rows.
    # none_as_null: Python None is stored as SQL NULL, not as the JSON value null,
    # so "error_details IS NOT NULL" checks mean what they say.
    error_details: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB(none_as_null=True))
    # File name of the generated PDF inside CERTIFICATE_STORAGE_DIR; set with SUCCESS.
    storage_key: Mapped[str | None] = mapped_column(String(255))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    # Processing timestamps; both stay NULL for INVALID rows, which are never processed.
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    job: Mapped[GenerationJob] = relationship(back_populates="recipients")
