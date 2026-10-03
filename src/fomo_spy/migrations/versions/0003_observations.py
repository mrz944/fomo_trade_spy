"""Persist displayed observations separately from executable trading events."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "observations",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("received", sa.Float(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
    )
    op.create_index("ix_observations_received", "observations", ["received"])


def downgrade():
    raise RuntimeError("Restore a verified backup to downgrade audit storage")
