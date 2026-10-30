"""epochs, allocations (Stage 11)

Revision ID: 0003
Revises: 0002
"""
from alembic import op

from app.db.models import Base

revision = "0003"
down_revision = "0002"


def upgrade() -> None:
    Base.metadata.create_all(op.get_bind())  # only creates the tables that do not exist yet


def downgrade() -> None:
    op.drop_table("allocations")
    op.drop_table("epochs")
