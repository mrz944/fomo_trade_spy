"""Research selection and current inventory, independent of historical qualification."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("research_selections", "monitor_states"):
        op.create_table(
            table,
            sa.Column("key", sa.String(), primary_key=True),
            sa.Column("data", sa.JSON(), nullable=False),
        )
    op.add_column(
        "positions",
        sa.Column("selection_policy", sa.String(), nullable=False, server_default="verified"),
    )
    # JSON provenance is additive. Existing execution accounting remains unchanged.
    for table, column in (("orders", "data"), ("ledger", "details")):
        op.execute(
            sa.text(
                f"UPDATE {table} SET {column}=json_set({column}, '$.selection_policy', 'verified') WHERE json_extract({column}, '$.selection_policy') IS NULL"
            )
        )


def downgrade():
    raise RuntimeError("Restore a verified pre-trading backup to downgrade")
