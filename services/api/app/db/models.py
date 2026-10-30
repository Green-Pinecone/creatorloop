import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    TypeDecorator,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Wei(TypeDecorator):
    """numeric(78,0) on Postgres; decimal string on SQLite (which would round big ints). Python side is int."""

    impl = Numeric(78, 0)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(String(80) if dialect.name == "sqlite" else Numeric(78, 0))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return str(int(value)) if dialect.name == "sqlite" else Decimal(int(value))

    def process_result_value(self, value, dialect):
        return None if value is None else int(value)


TS = DateTime(timezone=True)
ACCOUNT_STATUSES = ("active", "restricted", "frozen", "banned")


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    privy_user_id: Mapped[str] = mapped_column(String, unique=True)
    wallet_address: Mapped[str | None] = mapped_column(String, unique=True)
    email_hash: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="active")
    trust_score: Mapped[float] = mapped_column(default=0.5)
    trust_updated_at: Mapped[datetime | None] = mapped_column(TS)
    risk_flags: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class Creator(Base):
    __tablename__ = "creators"
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    handle: Mapped[str] = mapped_column(String, unique=True)
    display_name: Mapped[str] = mapped_column(String)
    bio: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class Post(Base):
    __tablename__ = "posts"
    post_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    author_wallet: Mapped[str] = mapped_column(String)
    account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("accounts.id"))
    metadata_cid: Mapped[str] = mapped_column(String)
    media_sha256: Mapped[str | None] = mapped_column(String)
    caption: Mapped[str | None] = mapped_column(Text)
    moderation: Mapped[str] = mapped_column(String, default="pending")
    hidden: Mapped[bool] = mapped_column(default=False)
    boost_until: Mapped[datetime | None] = mapped_column(TS)
    block_number: Mapped[int] = mapped_column(BigInteger)
    tx_hash: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(TS)
    __table_args__ = (Index("ix_posts_created", "created_at"),)


class Reaction(Base):
    __tablename__ = "reactions"
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.post_id"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class Follow(Base):
    __tablename__ = "follows"
    follower_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    creator_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    __table_args__ = (CheckConstraint("follower_id <> creator_id"),)


class Tip(Base):
    __tablename__ = "tips"
    tx_hash: Mapped[str] = mapped_column(String, primary_key=True)
    log_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.post_id"))
    from_wallet: Mapped[str] = mapped_column(String)
    to_wallet: Mapped[str] = mapped_column(String)
    amount_wei: Mapped[int] = mapped_column(Wei)
    block_number: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(TS)


class DeviceSignal(Base):
    __tablename__ = "device_signals"
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    device_hash: Mapped[str] = mapped_column(String, primary_key=True)
    ip_bucket_hash: Mapped[str] = mapped_column(String, primary_key=True)
    ua_family: Mapped[str | None] = mapped_column(String)
    first_seen: Mapped[datetime] = mapped_column(TS, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(TS, default=utcnow)
    hits: Mapped[int] = mapped_column(default=1)


class WalletFunder(Base):
    __tablename__ = "wallet_funders"
    wallet: Mapped[str] = mapped_column(String, primary_key=True)
    funder: Mapped[str] = mapped_column(String)
    tx_hash: Mapped[str] = mapped_column(String)
    amount_wei: Mapped[int] = mapped_column(Wei)
    funded_at: Mapped[datetime] = mapped_column(TS)


class ChainCursor(Base):
    __tablename__ = "chain_cursor"
    name: Mapped[str] = mapped_column(String, primary_key=True)
    last_block: Mapped[int] = mapped_column(BigInteger)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    provider: Mapped[str] = mapped_column(String, primary_key=True)
    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    received_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    payload: Mapped[dict] = mapped_column(JSON)
    processed_at: Mapped[datetime | None] = mapped_column(TS)


class Outbox(Base):
    __tablename__ = "outbox"
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    topic: Mapped[str] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    available_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    done_at: Mapped[datetime | None] = mapped_column(TS)
    __table_args__ = (Index("ix_outbox_pending", "available_at", "done_at"),)


class FraudCase(Base):
    __tablename__ = "fraud_cases"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String)  # linkage_cluster | collusion_ring | clone_pairs | payment_abuse
    status: Mapped[str] = mapped_column(String, default="open")  # open|investigating|recommended|actioned|dismissed
    severity: Mapped[int] = mapped_column(Integer)
    fingerprint: Mapped[str] = mapped_column(String, unique=True)
    member_ids: Mapped[list] = mapped_column(JSON)
    evidence: Mapped[dict] = mapped_column(JSON)
    agent_report: Mapped[dict | None] = mapped_column(JSON)
    decision: Mapped[str | None] = mapped_column(String)
    decision_note: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("accounts.id"))
    decided_at: Mapped[datetime | None] = mapped_column(TS)
    opened_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class Payment(Base):
    __tablename__ = "payments"
    dodo_payment_id: Mapped[str] = mapped_column(String, primary_key=True)
    account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("accounts.id"))
    creator_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("accounts.id"))
    kind: Mapped[str] = mapped_column(String)  # supporter_pass | boost | creator_pro
    status: Mapped[str] = mapped_column(String)  # succeeded | failed | refunded | disputed
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String)
    dodo_customer_id: Mapped[str | None] = mapped_column(String)
    payer_hash: Mapped[str | None] = mapped_column(String)
    subscription_id: Mapped[str | None] = mapped_column(String)
    post_id: Mapped[int | None] = mapped_column(BigInteger)
    raw: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class Subscription(Base):
    __tablename__ = "subscriptions"
    dodo_subscription_id: Mapped[str] = mapped_column(String, primary_key=True)
    account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("accounts.id"))
    creator_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("accounts.id"))
    status: Mapped[str] = mapped_column(String)  # active | on_hold | cancelled | expired | failed
    current_period_end: Mapped[datetime | None] = mapped_column(TS)
    updated_at: Mapped[datetime] = mapped_column(TS, default=utcnow)


class LedgerEntry(Base):
    """Double entry: the entries of one txn_id always sum to zero. +credit / -debit, integer minor units."""

    __tablename__ = "ledger_entries"
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    txn_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    account: Mapped[str] = mapped_column(String, index=True)  # cash:dodo | creator_fund:<uuid> | platform_revenue | refunds
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String)
    held: Mapped[bool] = mapped_column(default=False)
    ref: Mapped[str] = mapped_column(String, index=True)  # dodo_payment_id / refund id
    created_at: Mapped[datetime] = mapped_column(TS, default=utcnow)
