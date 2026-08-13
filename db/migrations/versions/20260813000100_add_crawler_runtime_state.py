"""Add continuous crawler runtime state and attempt history.

Revision ID: 20260813000100
Revises: 20260811000200
"""

from alembic import op
import sqlalchemy as sa


revision = "20260813000100"
down_revision = "20260811000200"
branch_labels = None
depends_on = None


OUTCOMES = "'running', 'succeeded', 'failed', 'timed_out', 'launch_failed', 'interrupted'"


def upgrade() -> None:
    op.create_table(
        "crawler_runtime_state",
        sa.Column("crawler_path", sa.Text(), primary_key=True),
        sa.Column("last_attempt_started_at", sa.DateTime(timezone=True)),
        sa.Column("last_attempt_finished_at", sa.DateTime(timezone=True)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_outcome", sa.String()),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_owner", sa.Text()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            f"last_outcome IS NULL OR last_outcome IN ({OUTCOMES})",
            name="ck_crawler_runtime_state_outcome",
        ),
        sa.CheckConstraint(
            "consecutive_failures >= 0",
            name="ck_crawler_runtime_state_failures",
        ),
    )
    op.create_index(
        "ix_crawler_runtime_state_due",
        "crawler_runtime_state",
        ["last_attempt_started_at", "crawler_path"],
    )
    op.create_index(
        "ix_crawler_runtime_state_lease",
        "crawler_runtime_state",
        ["lease_expires_at"],
    )

    op.create_table(
        "crawler_runtime_attempt",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "crawler_path",
            sa.Text(),
            sa.ForeignKey("crawler_runtime_state.crawler_path", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("worker_id", sa.Text(), nullable=False),
        sa.Column("outcome", sa.String(), nullable=False, server_default="running"),
        sa.Column("return_code", sa.Integer()),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            f"outcome IN ({OUTCOMES})",
            name="ck_crawler_runtime_attempt_outcome",
        ),
    )
    op.create_index(
        "ix_crawler_runtime_attempt_path_started",
        "crawler_runtime_attempt",
        ["crawler_path", "started_at"],
    )
    op.create_index(
        "ix_crawler_runtime_attempt_finished",
        "crawler_runtime_attempt",
        ["finished_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_crawler_runtime_attempt_finished", table_name="crawler_runtime_attempt")
    op.drop_index("ix_crawler_runtime_attempt_path_started", table_name="crawler_runtime_attempt")
    op.drop_table("crawler_runtime_attempt")
    op.drop_index("ix_crawler_runtime_state_lease", table_name="crawler_runtime_state")
    op.drop_index("ix_crawler_runtime_state_due", table_name="crawler_runtime_state")
    op.drop_table("crawler_runtime_state")
