"""Harden event inclusion assessment and quarantine.

Revision ID: 20260811000200
Revises: 20260811000100
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260811000200"
down_revision = "20260811000100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "classical_concert",
        sa.Column(
            "inclusion_status",
            sa.String(),
            nullable=False,
            server_default="included",
        ),
    )
    op.create_check_constraint(
        "ck_classical_concert_inclusion_status",
        "classical_concert",
        "inclusion_status IN ('included', 'quarantined', 'rejected')",
    )
    op.create_index(
        "ix_classical_concert_inclusion_date",
        "classical_concert",
        ["inclusion_status", "date", "id"],
    )

    for name in (
        "repaired_count",
        "blocked_promotion_count",
        "promoted_count",
        "shadow_classical_count",
    ):
        op.add_column(
            "potential_event_classification_run",
            sa.Column(name, sa.Integer(), nullable=False, server_default="0"),
        )

    op.create_table(
        "event_inclusion_assessment",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "potential_event_id",
            sa.Integer(),
            sa.ForeignKey("potential_event.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "classical_concert_id",
            sa.Integer(),
            sa.ForeignKey("classical_concert.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "classification_run_id",
            sa.BigInteger(),
            sa.ForeignKey("potential_event_classification_run.id", ondelete="SET NULL"),
        ),
        sa.Column("origin", sa.String(), nullable=False),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column(
            "evidence_urls",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("source_url", sa.Text()),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "potential_event_id IS NOT NULL OR classical_concert_id IS NOT NULL",
            name="ck_event_inclusion_assessment_target",
        ),
        sa.CheckConstraint(
            "origin IN ('potential_classifier', 'programme_analyzer', 'manual')",
            name="ck_event_inclusion_assessment_origin",
        ),
        sa.CheckConstraint(
            "decision IN ('classical', 'nonclassical', 'not_event', 'uncertain')",
            name="ck_event_inclusion_assessment_decision",
        ),
    )
    op.create_index(
        "ix_event_inclusion_assessment_potential",
        "event_inclusion_assessment",
        ["potential_event_id", "created_at"],
    )
    op.create_index(
        "ix_event_inclusion_assessment_concert",
        "event_inclusion_assessment",
        ["classical_concert_id", "created_at"],
    )
    op.create_index(
        "ix_event_inclusion_assessment_decision",
        "event_inclusion_assessment",
        ["decision", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_event_inclusion_assessment_decision",
        table_name="event_inclusion_assessment",
    )
    op.drop_index(
        "ix_event_inclusion_assessment_concert",
        table_name="event_inclusion_assessment",
    )
    op.drop_index(
        "ix_event_inclusion_assessment_potential",
        table_name="event_inclusion_assessment",
    )
    op.drop_table("event_inclusion_assessment")
    for name in (
        "shadow_classical_count",
        "promoted_count",
        "blocked_promotion_count",
        "repaired_count",
    ):
        op.drop_column("potential_event_classification_run", name)
    op.drop_index(
        "ix_classical_concert_inclusion_date",
        table_name="classical_concert",
    )
    op.drop_constraint(
        "ck_classical_concert_inclusion_status",
        "classical_concert",
        type_="check",
    )
    op.drop_column("classical_concert", "inclusion_status")
