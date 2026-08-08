"""restore the composite city identity constraint for rollout compatibility

Revision ID: 20260808000200
Revises: 20260808000100
Create Date: 2026-08-08 00:02:00
"""

from typing import Sequence, Union

from alembic import op


revision: str = "20260808000200"
down_revision: Union[str, None] = "20260808000100"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_city_external_identity", "city", ["external_source", "external_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_city_external_identity", "city", type_="unique")
