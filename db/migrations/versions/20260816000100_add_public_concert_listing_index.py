"""Add a covering index for public concert listings.

Revision ID: 20260816000100
Revises: 20260813000100
"""

from alembic import op
import sqlalchemy as sa


revision = "20260816000100"
down_revision = "20260813000100"
branch_labels = None
depends_on = None


INDEX_NAME = "ix_classical_concert_public_listing"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            INDEX_NAME,
            "classical_concert",
            ["date", "time_from", "id"],
            postgresql_concurrently=True,
            postgresql_include=["city_id", "country_code_resolved"],
            postgresql_where=sa.text("inclusion_status = 'included'"),
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            INDEX_NAME,
            table_name="classical_concert",
            postgresql_concurrently=True,
        )
