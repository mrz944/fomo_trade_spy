"""Add evidence storage without modifying the trading ledger."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("scan_checkpoints", "historical_valuations", "coverage_gaps"):
        op.create_table(
            name,
            sa.Column("key", sa.String(), primary_key=True),
            sa.Column("data", sa.JSON(), nullable=False),
        )
    op.create_table(
        "evidence_fills",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("trader", sa.String(), nullable=False),
        sa.Column("chain", sa.String(), nullable=False),
        sa.Column("timestamp", sa.Float(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
    )
    op.create_index("ix_evidence_fills_trader", "evidence_fills", ["trader"])
    op.create_table(
        "provider_swaps",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("trader", sa.String(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
    )
    op.create_index("ix_provider_swaps_trader", "provider_swaps", ["trader"])


def downgrade():
    raise RuntimeError("Evidence is audit data; restore a verified backup to downgrade")
