"""payments, subscriptions, ledger_entries, fraud_cases, posts.boost_until (Stages 05 and 07)

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op

from app.db.models import Base

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    bind = op.get_bind()
    # Idempotent: a database created from scratch at 0001 already has the current models.
    Base.metadata.create_all(bind)  # creates only the tables that do not exist yet
    cols = {c["name"] for c in sa.inspect(bind).get_columns("posts")}
    if "boost_until" not in cols:
        op.add_column("posts", sa.Column("boost_until", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for t in ("ledger_entries", "subscriptions", "payments", "fraud_cases"):
        op.drop_table(t)
    op.drop_column("posts", "boost_until")
