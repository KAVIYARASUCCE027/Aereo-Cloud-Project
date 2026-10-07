"""add optional recipient achievement

A nullable column, so existing rows and requests without an achievement are
unaffected. Like name and email, INVALID rows never store it.

Revision ID: 0e711d3894cc
Revises: 56bdc5b0374c
Create Date: 2026-10-07 21:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0e711d3894cc"
down_revision: str | None = "56bdc5b0374c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECK_NAME = "ck_job_recipients_invalid_has_no_achievement"


def upgrade() -> None:
    op.add_column("job_recipients", sa.Column("achievement", sa.String(length=150), nullable=True))
    op.create_check_constraint(op.f(CHECK_NAME), "job_recipients", "status <> 'INVALID' OR achievement IS NULL")


def downgrade() -> None:
    op.drop_constraint(op.f(CHECK_NAME), "job_recipients", type_="check")
    op.drop_column("job_recipients", "achievement")
