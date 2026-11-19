"""per-account stats privacy opt-out

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-19

SQLite cannot rebuild `users` while the `effective_users` view references it, so the view is
dropped for the column add and recreated afterwards (same SQL as 0001).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0005"
down_revision: Union[str, Sequence[str], None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

EFFECTIVE_USERS_VIEW_SQL = """
CREATE VIEW effective_users AS
WITH RECURSIVE chain(id, effective_id, depth) AS (
    SELECT id, id, 0 FROM users WHERE merged_into_id IS NULL
    UNION ALL
    SELECT u.id, chain.effective_id, chain.depth + 1
    FROM users u JOIN chain ON u.merged_into_id = chain.id
    WHERE chain.depth < 64
)
SELECT id, effective_id FROM chain
"""


def upgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS effective_users"))
    op.add_column(
        "users",
        sa.Column("stats_private", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "pending_registrations",
        sa.Column("stats_private", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    )
    op.execute(sa.text(EFFECTIVE_USERS_VIEW_SQL))


def downgrade() -> None:
    op.execute(sa.text("DROP VIEW IF EXISTS effective_users"))
    op.drop_column("pending_registrations", "stats_private")
    op.drop_column("users", "stats_private")
    op.execute(sa.text(EFFECTIVE_USERS_VIEW_SQL))
