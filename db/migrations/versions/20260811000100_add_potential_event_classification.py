"""add Codex potential event classification state

Revision ID: 20260811000100
Revises: 20260808000300
Create Date: 2026-08-11 00:01:00
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "20260811000100"
down_revision: Union[str, None] = "20260808000300"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "potential_event_classification_run",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("source", sa.Text()),
        sa.Column("source_url", sa.Text()),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="running"),
        sa.Column("thread_id", sa.Text()),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("snapshot_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("classified_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("uncertain_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "findings",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("last_error", sa.Text()),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "scope IN ('future', 'all')",
            name="ck_potential_event_classification_run_scope",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'partial', 'failed')",
            name="ck_potential_event_classification_run_status",
        ),
    )
    op.create_index(
        "ix_potential_event_classification_run_source",
        "potential_event_classification_run",
        ["source", "source_url", "started_at"],
    )

    op.create_table(
        "potential_event_classification",
        sa.Column(
            "potential_event_id",
            sa.Integer(),
            sa.ForeignKey("potential_event.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "latest_run_id",
            sa.BigInteger(),
            sa.ForeignKey("potential_event_classification_run.id", ondelete="SET NULL"),
        ),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("is_classical", sa.Boolean()),
        sa.Column("category", sa.String()),
        sa.Column("rationale", sa.Text()),
        sa.Column(
            "evidence_urls",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("model", sa.String()),
        sa.Column("last_error", sa.Text()),
        sa.Column("last_attempted_at", sa.DateTime(timezone=True)),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('classified', 'uncertain', 'error', 'failed')",
            name="ck_potential_event_classification_status",
        ),
        sa.CheckConstraint(
            "(status = 'classified' AND is_classical IS NOT NULL) OR "
            "(status <> 'classified' AND is_classical IS NULL)",
            name="ck_potential_event_classification_decision",
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name="ck_potential_event_classification_attempts",
        ),
    )
    op.create_index(
        "ix_potential_event_classification_due",
        "potential_event_classification",
        ["status", "next_attempt_at", "attempts"],
    )
    op.create_index(
        "ix_potential_event_classification_run",
        "potential_event_classification",
        ["latest_run_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_potential_event_classification_run",
        table_name="potential_event_classification",
    )
    op.drop_index(
        "ix_potential_event_classification_due",
        table_name="potential_event_classification",
    )
    op.drop_table("potential_event_classification")
    op.drop_index(
        "ix_potential_event_classification_run_source",
        table_name="potential_event_classification_run",
    )
    op.drop_table("potential_event_classification_run")
