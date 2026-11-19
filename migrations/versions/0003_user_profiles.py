"""user profile extras: status text and profile picture

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_profiles",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("status_text", sa.String(length=60), nullable=True),
        sa.Column("avatar", sa.LargeBinary(), nullable=True),
        sa.Column("avatar_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status_text IS NULL OR length(status_text) BETWEEN 1 AND 60",
            name=op.f("ck_user_profiles_status_text_length"),
        ),
        sa.CheckConstraint(
            "avatar IS NULL OR length(avatar) BETWEEN 1 AND 200000", name=op.f("ck_user_profiles_avatar_size")
        ),
        sa.CheckConstraint(
            "(avatar IS NULL) = (avatar_updated_at IS NULL)", name=op.f("ck_user_profiles_avatar_timestamp")
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_user_profiles_user_id_users")),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_user_profiles")),
    )


def downgrade() -> None:
    op.drop_table("user_profiles")
