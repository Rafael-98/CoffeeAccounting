"""achievements: unlocks and the public showcase

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "achievement_unlocks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=40), nullable=False),
        sa.Column("unlocked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(key) > 0", name=op.f("ck_achievement_unlocks_key_nonempty")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_achievement_unlocks_user_id_users")),
        sa.ForeignKeyConstraint(
            ["event_id"], ["consumption_events.id"], name=op.f("fk_achievement_unlocks_event_id_consumption_events")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_achievement_unlocks")),
    )
    op.create_index("uq_achievement_unlocks_user_key", "achievement_unlocks", ["user_id", "key"], unique=True)
    op.create_index(op.f("ix_achievement_unlocks_user_id"), "achievement_unlocks", ["user_id"], unique=False)
    op.create_index(op.f("ix_achievement_unlocks_event_id"), "achievement_unlocks", ["event_id"], unique=False)
    op.create_table(
        "achievement_showcase",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=40), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_achievement_showcase_user_id_users")),
        sa.PrimaryKeyConstraint("user_id", "key", name=op.f("pk_achievement_showcase")),
    )


def downgrade() -> None:
    op.drop_table("achievement_showcase")
    op.drop_index(op.f("ix_achievement_unlocks_event_id"), table_name="achievement_unlocks")
    op.drop_index(op.f("ix_achievement_unlocks_user_id"), table_name="achievement_unlocks")
    op.drop_index("uq_achievement_unlocks_user_key", table_name="achievement_unlocks")
    op.drop_table("achievement_unlocks")
